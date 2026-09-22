// 写作台的深改姿态与「AI 放在抽屉里」时的续写面板。
// 深改只诊断、不代笔：诊断是服务端的一份（GET /api/v1/scenes/{id}/deep-review），每一项给
// 「选中这一句去改写」/「按诊断改写」，回到起草姿态并选中那一句——改写走选区工具条，并带着这条发现。
// 过去这里挂着三条本地正则和一条「采纳候选 · 写回正文」的通路，诊断从不产出候选，那条路永远走不到。
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

/* ---- 服务端诊断的夹具：一条节奏发现钉在第 1 段的「安静，安静」上 ---- */
const ECHO = {
  signal_id: "craft:adjacent_echo:11111111", quality_signal_id: "craft:adjacent_echo:11111111",
  source: "craft", dimension: "adjacent_echo", label: "贴邻叠句", lens: null, severity: "taste",
  issue: "「安静，安静」贴邻重复。", recommendation: "短语回响节奏偏刻意，考虑改换连接或删一处。", why: "",
  evidence: { excerpt: "安静，安静", paragraph_index: 0, start: 3, end: 8 },
  context: "门外很安静，安静到能听见潮水。", ignored: false, stale: false, house_taste: false, origin: null,
  patch: { candidate_category: "action_replace", revision_strategy: "短语回响节奏偏刻意，考虑改换连接或删一处。" },
};
/* 整场缺席的发现：没有位置，只列出 */
const ABSENCE = {
  signal_id: "rules:no_choice_scene:scene", quality_signal_id: "rules:no_choice_scene:scene",
  source: "rules", dimension: "no_choice_scene", label: "无抉择场景", lens: null, severity: "revision",
  issue: "这一场在纸面上看不到明确的抉择。", recommendation: "给人物两个不能兼得的选项，让其中一个看得见地付出代价。", why: "",
  evidence: null, context: "门外很安静，安静到能听见潮水。", ignored: false, stale: false, house_taste: false, origin: null,
  patch: { candidate_category: "local_patch", revision_strategy: "…" },
};
const AI_FINDING = {
  signal_id: "ai:choice_pressure:99999999", quality_signal_id: "ai:choice_pressure:99999999",
  source: "ai", dimension: "choice_pressure", label: "抉择压力", lens: "story", severity: "revision",
  issue: "选择被说出来了，没有落成动作。", recommendation: "让她把证据袋交出去，或者锁起来。", why: "读者要看见代价。",
  evidence: { excerpt: "能听见潮水", paragraph_index: 0, start: 9, end: 14 },
  context: "门外很安静，安静到能听见潮水。", ignored: false, stale: false, house_taste: false,
  origin: { evaluation_id: "writer_deep_eval_1", created_at: "2026-09-22T10:00:00Z" },
  patch: { candidate_category: "local_patch", revision_strategy: "让她把证据袋交出去，或者锁起来。" },
};

function diagnosisPayload(findings = [ECHO], extra = {}) {
  const open = findings.filter((f) => !f.ignored);
  return {
    scene_id: "s1", chapter_id: "c1", project_id: "prj-main",
    text: { layer: "author_draft", ref: "author_draft:d1", sha256: "x", paragraph_count: 1, chars: 14 },
    style_bound: false,
    findings,
    summary: { total: findings.length, open: open.length, ignored: findings.length - open.length, stale: 0, by_severity: {}, by_source: {} },
    ai: { status: "not_run", evaluation_id: null, overall_score: null, revision_brief: [], lenses: [], llm_call_id: null, created_at: null },
    review: { status: "not_run", evaluation_id: null, overall_score: null, failure_class: null, revision_brief: [], created_at: null },
    preferences: { revision_no: 0, decision_log: [], ignored_issue_keys: [] },
    patch_candidates: [],
    status: "not_run", object_type: "scene", object_id: "s1", rubric_id: "literary_revision_v1",
    latest_evaluation: null, latest_score: null, requires_human_review: false, lens_evaluations: [],
    ...extra,
  };
}

/* diagnosis：GET 的答复；aiRun：POST（AI 深评）的答复或要抛的错误；patch：改写接口的答复 */
async function loadWriter({ diagnosis = diagnosisPayload(), aiRun = null, patch = null } = {}) {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  const baseGet = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => {
    const target = String(url);
    if (/\/deep-review$/.test(target)) return Promise.resolve(typeof diagnosis === "function" ? diagnosis(target) : diagnosis);
    if (/\/deep-review\/preferences$/.test(target)) return Promise.resolve({ decision_log: [], ignored_issue_keys: [], revision_no: 0 });
    return baseGet(url);
  });
  client.apiPost.mockImplementation((url, body) => {
    const target = String(url);
    if (/\/deep-review$/.test(target)) {
      if (aiRun instanceof Error) return Promise.reject(aiRun);
      return Promise.resolve(aiRun || (typeof diagnosis === "function" ? diagnosis(target) : diagnosis));
    }
    if (/\/passages\/patch-candidates$/.test(target)) {
      return Promise.resolve(patch || { candidate: { patch_id: "p1", replacement_options: [{ option_id: "o1", replacement_text: "门外很安静，静得能听见潮水。" }] } });
    }
    if (/\/passage-patch-candidates\//.test(target)) return Promise.resolve({});
    return Promise.resolve({});
  });
  /* 偏好 PATCH：像服务端那样把身体回显、修订号 +1（默认的 {} 会把本机忽略清单冲成空） */
  client.apiPatch.mockImplementation((url, body) => Promise.resolve({ ...(body || {}), revision_no: Number((body && body.base_revision_no) || 0) + 1 }));
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
const drawerButton = (host, text) => [...host.querySelectorAll(".wr-dxd button")].find((node) => node.textContent.includes(text));

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
  it("诊断来自服务端：进深改先取 GET deep-review，标注钉在那一句上；「选中这一句去改写」回到起草姿态并选中那一句；没有「采纳 · 写回正文」", async () => {
    const { WriterRoom, WrDocs, client } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const save = vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);

    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/scenes/s1/deep-review");
    expect(drawer.textContent).not.toMatch(/采纳|写回正文|没有现成候选|ECH|RDN/);
    expect(drawer.textContent).toContain("AI 深评");
    expect(host.querySelector(".wr-editor").getAttribute("contenteditable")).toBe("false");
    // 标注：mark.wr-dx 包着「安静，安静」，带发现 id；第一条默认是选中的那条
    const mark = host.querySelector('mark.wr-dx[data-dx="craft:adjacent_echo:11111111"]');
    expect(mark).not.toBeNull();
    expect(mark.textContent).toBe("安静，安静");
    expect(mark.classList.contains("is-active")).toBe(true);
    expect(mark.classList.contains("sev-taste")).toBe(true);

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click(drawerButton(host, "选中这一句去改写"));
      await act(async () => { vi.advanceTimersByTime(120); });
    } finally {
      vi.useRealTimers();
    }
    expect(host.querySelector(".wr-root").getAttribute("data-posture")).toBe("draft");
    expect(host.querySelector(".wr-editor").getAttribute("contenteditable")).toBe("true");
    expect(host.querySelector("mark.wr-dx")).toBeNull();
    expect(window.getSelection().toString()).toBe("安静，安静");
    // 深改进出都没有改动正文，所以一次也没存
    expect(save).not.toHaveBeenCalled();
  });

  it("从文学质量带着 signal_id 跳进来：诊断到了就选中同一条并高亮；找不到的 id 给一句提示", async () => {
    const { WriterRoom, WrDocs } = await loadWriter({ diagnosis: diagnosisPayload([ABSENCE, ECHO]) });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);

    await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: { posture: "deep", signal_id: ECHO.signal_id } })));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);
    // 默认本该停在第一条（无抉择场景）；带着 id 进来就停在那一条
    const active = drawer.querySelector('.wr-dxd-row[aria-pressed="true"]');
    expect(active.textContent).toContain("贴邻重复");
    expect(host.querySelector('mark.wr-dx.is-active[data-dx="craft:adjacent_echo:11111111"]')).not.toBeNull();
    expect(drawer.textContent).toContain("整场"); // 缺席的发现没有位置，只列出
    expect(drawer.textContent).not.toContain("没有对应位置");

    // 深改姿态里再来一个当前作者稿没有的 id：提示，不崩
    await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: { posture: "deep", signal_id: "rules:model_voice:deadbeef" } })));
    await vi.waitFor(() => expect(drawer.textContent).toContain("没有对应位置"), T);
  });

  it("忽略 / 恢复按 signal_id 存到服务端（带修订号），文学质量和成稿门读的就是这一份；重新诊断不丢忽略", async () => {
    const { WriterRoom, WrDocs, client } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);

    await click(drawerButton(host, "忽略这一项"));
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/deep-review/preferences",
      expect.objectContaining({ ignored_issue_keys: ["craft:adjacent_echo:11111111"], base_revision_no: 0 }),
    ), T);
    expect(drawer.textContent).toContain("本场没有发现待改的句段");
    expect(drawer.textContent).toContain("已忽略 1 项");
    expect(host.querySelector("mark.wr-dx")).toBeNull();
    expect(drawer.textContent).toContain("忽略 · 贴邻叠句");

    await click(drawerButton(host, "已忽略 1 项"));
    await click(drawerButton(host, "恢复"));
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenLastCalledWith(
      "/api/v1/scenes/s1/deep-review/preferences",
      expect.objectContaining({ ignored_issue_keys: [], base_revision_no: 1 }),
    ), T);
    await vi.waitFor(() => expect(host.querySelector('mark.wr-dx[data-dx="craft:adjacent_echo:11111111"]')).not.toBeNull(), T);
    expect(drawer.textContent).not.toContain("已忽略 1 项");
    expect(drawer.textContent).toContain("恢复 · 贴邻叠句");
  });

  it("「AI 深评」调 POST deep-review，结果并入同一份清单并按来源可筛；无模型时给「去系统设置」", async () => {
    const ai = diagnosisPayload([ECHO, AI_FINDING], {
      ai: { status: "current", evaluation_id: "writer_deep_eval_1", overall_score: 0.58, revision_brief: [{ action: "把选择落成动作。" }], lenses: [], llm_call_id: "c1", created_at: "2026-09-22T10:00:00Z" },
      status: "reviewed",
    });
    const { WriterRoom, WrDocs, client } = await loadWriter({ aiRun: ai });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);
    expect(drawer.textContent).toContain("还没跑过");

    await click(drawerButton(host, "AI 深评"));
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/deep-review", {}), T);
    await vi.waitFor(() => expect(drawer.textContent).toContain("选择被说出来了"), T);
    expect(drawer.textContent).toContain("总分 58");
    expect(drawer.textContent).toContain("把选择落成动作。");
    expect(host.querySelector('mark.wr-dx[data-dx="ai:choice_pressure:99999999"]').textContent).toBe("能听见潮水");
    // 来源筛选：只看 AI
    const chip = [...drawer.querySelectorAll(".wr-dxd-chip")].find((node) => node.textContent.includes("AI 深评"));
    await click(chip);
    expect(drawer.textContent).toContain("选择被说出来了");
    expect(drawer.textContent).not.toContain("贴邻重复。");
    expect(drawer.textContent).toContain("AI 深评 · 故事");

    // 无模型：拒绝式 409 → 去系统设置
    const denied = Object.assign(new Error("需要模型"), { code: "WRITER_DEEP_REVIEW_LLM_REQUIRED", status: 409, details: { author_action: { target_view: "config" } } });
    const go = vi.fn();
    const second = await loadWriter({ aiRun: denied });
    vi.spyOn(second.WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host2 = await render(<second.WriterRoom t={{}} setTweak={() => {}} go={go} />);
    await vi.waitFor(() => expect(host2.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host2));
    await vi.waitFor(() => expect(host2.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);
    await click(drawerButton(host2, "AI 深评"));
    await vi.waitFor(() => expect(host2.querySelector(".wr-dxd").textContent).toContain("没有可用的模型"), T);
    await click(drawerButton(host2, "去系统设置"));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("「按诊断改写」：选中那一句回到起草，工具条按发现的改法直接出候选，改写请求带着发现的 id / 维度 / 改法", async () => {
    const { WriterRoom, WrDocs, client } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click(drawerButton(host, "按诊断改写"));
      await act(async () => { vi.advanceTimersByTime(120); });
    } finally {
      vi.useRealTimers();
    }
    expect(host.querySelector(".wr-root").getAttribute("data-posture")).toBe("draft");
    /* jsdom 在下一拍才发 selectionchange；工具条捕获选区后按发现自动改写，结果一到焦点进弹层
       （jsdom 里聚焦按钮会清掉文档选区，所以不在这里断言选区——请求里的 source_excerpt 就是选中的那一句） */
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/passages/patch-candidates", expect.objectContaining({
      object_id: "s1",
      source_excerpt: "安静，安静",
      issue_dimension: "adjacent_echo",
      quality_signal_id: "craft:adjacent_echo:11111111",
      issue_note: "「安静，安静」贴邻重复。",
      instruction: "短语回响节奏偏刻意，考虑改换连接或删一处。",
      candidate_category: "action_replace",
    })), T);
    await vi.waitFor(() => expect(document.querySelector(".wr-irw-pop").textContent).toContain("替换为第 1 版"), T);
    expect(document.querySelector(".wr-irw-pop").textContent).toContain("按诊断：贴邻叠句");
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
      await click(drawerButton(host, "选中这一句去改写"));
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
