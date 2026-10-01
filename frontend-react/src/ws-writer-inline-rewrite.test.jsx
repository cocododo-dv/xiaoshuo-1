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

/* ---------- 跨段改写按段换回（重评 R12 / 批准 #20b） ----------
   选了几段就按段送（一段一行）、候选的几段按段换回：第一段接在起始段选区之前那一截后面，最后一段接上结束段选区之后
   那一截，中间各成一段；原来的几段留着，「还原原文」原样放回。过去整段改写塞进一个 <span>：段落并成一段、改好的字
   落在段落外面，还原也拼不回原来的几段。 */
const THREE = "<p>雨城入夜。林昭把旧信压在案卷底下。</p><p>灯下的字迹很淡。她读到第三行停住了。</p><p>窗外有人敲门。</p>";

async function loadRoom({ doc = THREE, options = null, reply = null } = {}) {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  client.apiPost.mockImplementation((url) => {
    if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
      return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: doc } });
    }
    if (url === "/api/v1/passages/patch-candidates") {
      if (reply) return reply();
      return Promise.resolve({ candidate: { patch_id: "patch-1", replacement_options: options || [] } });
    }
    return Promise.resolve({});
  });
  const store = await import("./wr-doc-store.jsx");
  vi.spyOn(store.WrDocs, "load").mockReturnValue(doc);
  vi.spyOn(store.WrDocs, "save").mockResolvedValue({});
  const writer = await import("./ws-writer.jsx");
  return { client, ...writer, ...store };
}

/* 按「第几段、段里第几个字」选中：[段号, 偏移] → [段号, 偏移]（段里的字可能被标记拆成好几个文本节点） */
function pointIn(block, offset) {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  let seen = 0;
  let node;
  let last = null;
  while ((node = walker.nextNode())) {
    last = node;
    if (offset <= seen + node.nodeValue.length) return [node, offset - seen];
    seen += node.nodeValue.length;
  }
  return [last, last ? last.nodeValue.length : 0];
}
async function selectBetween(editor, [startBlock, startOffset], [endBlock, endOffset]) {
  const blocks = editor.querySelectorAll("p");
  const range = document.createRange();
  range.setStart(...pointIn(blocks[startBlock], startOffset));
  range.setEnd(...pointIn(blocks[endBlock], endOffset));
  await act(async () => {
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
}
const startsWith = (root, text) => [...root.querySelectorAll("button")].find((node) => node.textContent.trim().startsWith(text));
const looseRootText = (editor) => [...editor.childNodes].filter((node) => node.nodeType === 3 && node.nodeValue.trim());
const paragraphTexts = (editor) => [...editor.children].map((node) => node.textContent);

async function openRoom(opts) {
  const room = await loadRoom(opts);
  const host = await render(<room.WriterRoom t={{}} setTweak={() => {}} />);
  const editor = host.querySelector(".wr-editor");
  await vi.waitFor(() => expect(editor.textContent).toContain(opts && opts.expect ? opts.expect : "林昭"), T);
  // 载入后那一帧的「当前段」标记先落定（它看的是那一刻的选区）：之后比 innerHTML 才不受它影响
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
  return { ...room, host, editor };
}
async function rewriteWith(label = "润色") {
  await vi.waitFor(() => expect(startsWith(document.body, label)).toBeTruthy(), T);
  await click(startsWith(document.querySelector(".wr-irw-bar"), label));
}
async function replaceWithFirst() {
  await vi.waitFor(() => expect(startsWith(document.body, "替换为第 1 版")).toBeTruthy(), T);
  await click(startsWith(document.body, "替换为第 1 版"));
}
async function openRevAndRevert(editor) {
  await act(async () => { window.getSelection().removeAllRanges(); });
  await click(editor.querySelector(".wr-rev"));
  const pop = () => document.querySelector('.wr-irw-pop[aria-label="AI 改写的这一处"]');
  await vi.waitFor(() => expect(pop()).not.toBeNull(), T);
  await click(button(pop(), "还原原文"));
  return pop;
}

describe("写作台 · 跨段改写按段换回（#20b）", () => {
  it("跨两段选中 + 两段的候选：按段送去改写，候选按段显示；换回后段落各归各位，编辑器根上没有散字", async () => {
    const options = [{ option_id: "o1", paragraphs: ["她把旧信压回案卷。", "灯下的字迹淡得像水。"], replacement_text: "她把旧信压回案卷。\n灯下的字迹淡得像水。" }];
    const { client, editor } = await openRoom({ options });
    // 从第一段「林昭把旧信…」起，选到第二段「…很淡。」止
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await vi.waitFor(() => expect(rewriteCalls(client)).toHaveLength(1), T);
    expect(rewriteCalls(client)[0][1].source_excerpt).toBe("林昭把旧信压在案卷底下。\n灯下的字迹很淡。");
    await vi.waitFor(() => expect(document.querySelectorAll(".wr-irw-cand .wr-irw-para")).toHaveLength(2), T);
    expect(document.querySelector(".wr-irw-pop").classList.contains("is-wide")).toBe(true);

    await replaceWithFirst();

    expect([...editor.children].map((node) => node.tagName)).toEqual(["P", "P", "P"]);
    expect(paragraphTexts(editor)).toEqual(["雨城入夜。她把旧信压回案卷。", "灯下的字迹淡得像水。她读到第三行停住了。", "窗外有人敲门。"]);
    expect(looseRootText(editor)).toEqual([]);
    expect([...editor.querySelectorAll(".wr-rev")].map((span) => span.textContent)).toEqual(["她把旧信压回案卷。", "灯下的字迹淡得像水。"]);
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/passage-patch-candidates/patch-1/accept", { selected_option_id: "o1" });
  });

  it("只选一段、「对话化」改出两句对白：换回成两段 <p>，不挤在一段里", async () => {
    const options = [{ option_id: "o1", paragraphs: ["“信是谁送来的？”林昭问。", "“不知道。”门外的人说。"] }];
    const { client, editor } = await openRoom({ options });
    await selectBetween(editor, [2, 0], [2, 7]);
    await rewriteWith("对话化");
    await vi.waitFor(() => expect(rewriteCalls(client)).toHaveLength(1), T);
    expect(rewriteCalls(client)[0][1].source_excerpt).toBe("窗外有人敲门。");

    await replaceWithFirst();

    expect([...editor.children].map((node) => node.tagName)).toEqual(["P", "P", "P", "P"]);
    expect(paragraphTexts(editor).slice(2)).toEqual(["“信是谁送来的？”林昭问。", "“不知道。”门外的人说。"]);
    expect(looseRootText(editor)).toEqual([]);
  });

  it("「还原原文」把原来的几段原样放回：编辑器的 innerHTML 逐字相同（加粗、斜体都在）", async () => {
    const doc = "<p>雨城入夜。<strong>林昭</strong>把旧信压在案卷底下。</p><p><em>灯下</em>的字迹很淡。她读到第三行停住了。</p>";
    const options = [{ option_id: "o1", paragraphs: ["她把信压回去。", "字迹淡得像水。"] }];
    const { editor } = await openRoom({ doc, options });
    const before = editor.innerHTML;
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await replaceWithFirst();
    expect(editor.innerHTML).not.toBe(before);

    await openRevAndRevert(editor);

    expect(editor.innerHTML).toBe(before);
    expect(editor.querySelector(".wr-rev")).toBeNull();
    expect(document.querySelector(".wr-irw-pop")).toBeNull();
  });

  it("候选出来前选中的第二段被改了：不替换（说清楚原因），正文原样", async () => {
    const options = [{ option_id: "o1", paragraphs: ["她把旧信压回案卷。", "灯下的字迹淡得像水。"] }];
    const { editor } = await openRoom({ options });
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await vi.waitFor(() => expect(startsWith(document.body, "替换为第 1 版")).toBeTruthy(), T);
    // 作者在等候选时改了第二段被选中的字
    await act(async () => {
      const text = editor.querySelectorAll("p")[1].firstChild;
      text.nodeValue = text.nodeValue.replace("很淡", "很浓");
    });
    const after = editor.innerHTML;

    await click(startsWith(document.body, "替换为第 1 版"));

    expect(document.querySelector(".wr-irw-pop").textContent).toContain("选中的那段字已经不在原处");
    expect(editor.innerHTML).toBe(after);
    expect(editor.querySelector(".wr-rev")).toBeNull();
  });

  it("改写之后又在起始段前半截、结束段后半截写了字：还原时这些字留着，选中的字按原来的段落回来", async () => {
    const options = [{ option_id: "o1", paragraphs: ["她把旧信压回案卷。", "灯下的字迹淡得像水。"] }];
    const { editor } = await openRoom({ options });
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await replaceWithFirst();
    await act(async () => {
      const [first, second] = editor.querySelectorAll("p");
      first.insertBefore(document.createTextNode("夜里，"), first.firstChild);
      second.appendChild(document.createTextNode("她又读了一遍。"));
    });

    await openRevAndRevert(editor);

    expect(paragraphTexts(editor)).toEqual([
      "夜里，雨城入夜。林昭把旧信压在案卷底下。",
      "灯下的字迹很淡。她读到第三行停住了。她又读了一遍。",
      "窗外有人敲门。",
    ]);
    expect(editor.querySelector(".wr-rev")).toBeNull();
  });

  it("改写的那几段本身又改过：不整段还原，说「可在版本历史里找回」，正文不动", async () => {
    const options = [{ option_id: "o1", paragraphs: ["她把旧信压回案卷。", "灯下的字迹淡得像水。"] }];
    const { editor } = await openRoom({ options });
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await replaceWithFirst();
    await act(async () => { editor.querySelectorAll(".wr-rev")[1].textContent = "灯下的字迹淡得像水，又像雾。"; });
    const edited = editor.innerHTML;

    const pop = await openRevAndRevert(editor);

    expect(pop().textContent).toContain("改写后又改过这几段，不能整段还原；可在版本历史里找回");
    expect(button(pop(), "还原原文").disabled).toBe(true);
    expect(editor.innerHTML).toBe(edited);
  });

  it("被选区切开的实体高亮拆掉、批注划线跟着剩下的字、诊断高亮不留；还原时原样回来", async () => {
    const { editor } = await openRoom({
      options: [{ option_id: "o1", paragraphs: ["她把旧信压回去。", "字迹淡了。"] }],
    });
    await act(async () => {
      editor.innerHTML = '<p>雨城入夜。<span class="wr-entity" data-lib-id="e1">林昭</span>把旧信压在案卷底下。</p>'
        + '<p>灯下的<mark class="wr-anno" data-anno-id="a1">字迹很淡</mark>。<mark class="wr-dx" data-dx="x">她读到</mark>第三行停住了。</p>';
    });
    const before = editor.innerHTML;
    // 从「林|昭」中间起，选到第二段「字迹|很淡」中间止
    await selectBetween(editor, [0, 6], [1, 5]);
    await rewriteWith();
    await replaceWithFirst();

    const [first, second] = editor.querySelectorAll("p");
    expect(first.textContent).toBe("雨城入夜。林她把旧信压回去。");
    expect(first.querySelector(".wr-entity")).toBeNull();          // 只剩半个名字：不再标成那份档案
    expect(second.textContent).toBe("字迹淡了。很淡。她读到第三行停住了。");
    expect(second.querySelector('mark.wr-anno[data-anno-id="a1"]').textContent).toBe("很淡");
    expect(editor.querySelector("mark.wr-dx")).toBeNull();
    expect(looseRootText(editor)).toEqual([]);

    await openRevAndRevert(editor);
    expect(editor.innerHTML).toBe(before);
  });

  it("选了两段、这一版却挤成了一段：这一版不给替换；一版都不剩时说「挤成了一段」，并回传弃用", async () => {
    const mixed = [
      { option_id: "o1", paragraphs: ["她把旧信压回案卷，灯下的字迹淡得像水。"] },
      { option_id: "o2", paragraphs: ["她把旧信压回案卷。", "灯下的字迹淡得像水。"] },
    ];
    const { client, editor } = await openRoom({ options: mixed });
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await vi.waitFor(() => expect(document.querySelectorAll(".wr-irw-cand")).toHaveLength(1), T);
    expect(document.querySelector(".wr-irw-cand").textContent).toContain("灯下的字迹淡得像水。");
    await click(startsWith(document.body, "替换为第 1 版"));
    expect(paragraphTexts(editor).slice(0, 2)).toEqual(["雨城入夜。她把旧信压回案卷。", "灯下的字迹淡得像水。她读到第三行停住了。"]);
    // 采纳回传的是作者选的那一版（o2），不是服务端列表里的第一个
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/passage-patch-candidates/patch-1/accept", { selected_option_id: "o2" });
  });

  it("每一版都挤成了一段：不替换，说「挤成了一段」，并回传弃用", async () => {
    const { client, editor } = await openRoom({ options: [{ option_id: "o1", paragraphs: ["她把旧信压回案卷，灯下的字迹淡得像水。"] }] });
    const before = editor.innerHTML;
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await vi.waitFor(() => expect(document.querySelector(".wr-irw-pop").textContent).toContain("挤成了一段"), T);
    expect(startsWith(document.body, "替换为第 1 版")).toBeUndefined();
    expect(editor.innerHTML).toBe(before);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/passage-patch-candidates/patch-1/reject", {}), T);
  });

  it("模型把几句对白放在了同一段（collapsed）：标一句提示，这一版照样可以替换", async () => {
    const options = [{ option_id: "o1", paragraphs: ["“走吧。”“不走。”"], collapsed: true }];
    const { editor } = await openRoom({ options });
    await selectBetween(editor, [2, 0], [2, 7]);
    await rewriteWith("对话化");
    await vi.waitFor(() => expect(document.querySelector(".wr-irw-cand-note")).not.toBeNull(), T);
    expect(document.querySelector(".wr-irw-cand-note").textContent).toBe("这一版把几句对白放在了同一段");
    await replaceWithFirst();
    expect(paragraphTexts(editor)[2]).toBe("“走吧。”“不走。”");
  });

  it("服务端回 WRITER_PASSAGE_PATCH_EMPTY：说模型没给出可用的结果，可以重试", async () => {
    const empty = Object.assign(new Error("模型这次没有给出可用的改写。"), { code: "WRITER_PASSAGE_PATCH_EMPTY", status: 502, details: { reason: "no_options" } });
    const { editor } = await openRoom({ reply: () => Promise.reject(empty) });
    await selectBetween(editor, [2, 0], [2, 7]);
    await rewriteWith();
    const pop = () => document.querySelector(".wr-irw-pop");
    await vi.waitFor(() => expect(pop().textContent).toContain("模型这次没有给出可用的结果"), T);
    expect(button(pop(), "重试")).toBeTruthy();
    expect(pop().textContent).not.toContain("去系统设置");
  });
});

describe("写作台 · 改写标记的原文跟着正文走，不跟着组件走", () => {
  it("工具条组件卸下再挂上（正文 DOM 还在）：点改写标记照样能还原原来的几段", async () => {
    const options = [{ option_id: "o1", paragraphs: ["她把旧信压回案卷。", "灯下的字迹淡得像水。"] }];
    await loadRoom({ options });
    const { WrInlineRewrite } = await import("./ws-writer-inline.jsx");
    const editor = document.createElement("div");
    editor.className = "wr-editor";
    editor.contentEditable = "true";
    editor.innerHTML = THREE;
    document.body.appendChild(editor);
    const editorRef = { current: editor };
    const before = editor.innerHTML;

    const first = await render(<WrInlineRewrite editorRef={editorRef} sceneId="ch01s1" annoKey={null} onCommit={() => {}} />);
    await selectBetween(editor, [0, 5], [1, 8]);
    await rewriteWith();
    await replaceWithFirst();
    expect(editor.querySelectorAll(".wr-rev")).toHaveLength(2);
    // 卸下这个组件（例如写作台重新挂载工具条）：正文 DOM 不动
    const mount = mounted.find((item) => item.host === first);
    await act(async () => mount.root.unmount());
    mounted.splice(mounted.indexOf(mount), 1);
    first.remove();

    await render(<WrInlineRewrite editorRef={editorRef} sceneId="ch01s1" annoKey={null} onCommit={() => {}} />);
    await openRevAndRevert(editor);

    expect(editor.innerHTML).toBe(before);
    editor.remove();
  });
});
