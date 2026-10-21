// 写作台的深改姿态与「AI 放在抽屉里」时的续写面板。
// 深改只诊断、不代笔：诊断是服务端的一份（GET /api/v1/scenes/{id}/deep-review），每一项给
// 「选中去改」/「按诊断改写」，回到起草姿态并选中那一句——改写走选区工具条，并带着这条发现。
// 过去这里挂着三条本地正则和一条「采纳候选 · 写回正文」的通路，诊断从不产出候选，那条路永远走不到。
// 2026-10-21 右栏重排：来源状态条（AI 深评 / 风格对照 / 参考书校准各一行，整体建议与校准折叠）、来源下拉、
// 按严重程度分组、展开的一条只有一个主按钮、已忽略与最近的决定折叠；深改时右栏有自己的宽度。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter, settleActiveWork, settleCatalog } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

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

/* 风格对照（对照检查的评审并进诊断，2026-10-20）：改法引的「能听见潮水」在第 1 段 */
const STYLE_FINDING = {
  signal_id: "style:theme.emotional_tone:abcd1234", source: "style", dimension: "theme.emotional_tone", label: "情感基调",
  lens: null, severity: "revision", issue: "写到潮水时语调忽然庄重。", recommendation: "在「能听见潮水」处，用一句自嘲压住抒情。",
  why: "对照检查：这一维 5.6 / 10，6 分以上算像（已按作者原文校准，评审 3 次取中位）。",
  evidence: { excerpt: "能听见潮水", paragraph_index: 0, start: 9, end: 14 }, context: "能听见潮水",
  ignored: false, stale: false, house_taste: false, related: null, opinion: null,
  origin: { kind: "style", reading_id: "sfr-1", created_at: "2026-10-20T06:00:00Z", score: 5.6, pass_score: 6, samples: 3 },
  patch: { candidate_category: "local_patch", revision_strategy: "在「能听见潮水」处，用一句自嘲压住抒情。" },
};
const STYLE_BLOCK = {
  bound: true, status: "current", reading_id: "sfr-1", created_at: "2026-10-20T06:00:00Z", draft_ref: "author_draft:d1:rev1",
  overall: 6.4, raw_overall: 7, pass_score: 6, judge_state: "calibrated", samples: 3, summary: "",
};

/* diagnosis：GET 的答复；aiRun：POST（AI 深评）的答复或要抛的错误；patch：改写接口的答复；check：发起对照检查的答复 */
async function loadWriter({ diagnosis = diagnosisPayload(), aiRun = null, patch = null, passage = null, check = null } = {}) {
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
    if (/\/deep-review\/passage$/.test(target)) {
      if (passage instanceof Error) return Promise.reject(passage);
      return Promise.resolve(typeof passage === "function" ? passage(body) : (passage || diagnosisPayload()));
    }
    if (/\/passages\/patch-candidates$/.test(target)) {
      return Promise.resolve(patch || { candidate: { patch_id: "p1", replacement_options: [{ option_id: "o1", replacement_text: "门外很安静，静得能听见潮水。" }] } });
    }
    if (/\/passage-patch-candidates\//.test(target)) return Promise.resolve({});
    if (/\/style-reference\/checks$/.test(target)) return Promise.resolve(typeof check === "function" ? check(body) : (check || {}));
    return Promise.resolve({});
  });
  /* 偏好 PATCH：像服务端那样把身体回显、修订号 +1（默认的 {} 会把本机忽略清单冲成空） */
  client.apiPatch.mockImplementation((url, body) => Promise.resolve({ ...(body || {}), revision_no: Number((body && body.base_revision_no) || 0) + 1 }));
  await import("./ws-catalog.jsx");
  await settleActiveWork("prj-main", T);
  await settleCatalog(T);
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
/* 来源下拉（过去是一排胶囊）：选一个来源 */
const sourceSelect = (host) => host.querySelector(".wr-dxd select.wr-dxd-filter");
const pickSource = (host, value) => act(async () => {
  const select = sourceSelect(host);
  select.value = value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
});
const railRight = (host) => host.querySelector(".wr-root").style.getPropertyValue("--rail-r");

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
  it("诊断来自服务端：进深改先取 GET deep-review，标注钉在那一句上；「选中去改」回到起草姿态并选中那一句；没有「采纳 · 写回正文」", async () => {
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
    // 标注：mark.wr-dx 包着「安静，安静」，带发现 id；进来不展开哪一条，标注也都没有选中
    const mark = host.querySelector('mark.wr-dx[data-dx="craft:adjacent_echo:11111111"]');
    expect(mark).not.toBeNull();
    expect(mark.textContent).toBe("安静，安静");
    expect(mark.classList.contains("is-active")).toBe(false);
    expect(mark.classList.contains("sev-taste")).toBe(true);
    expect(drawer.querySelector(".wr-dxd-detail")).toBeNull();
    // 点那一行：展开它，正文里那一句标成选中
    await click(drawer.querySelector(".wr-dxd-row"));
    expect(host.querySelector('mark.wr-dx[data-dx="craft:adjacent_echo:11111111"]').classList.contains("is-active")).toBe(true);

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click(drawerButton(host, "选中去改"));
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

  it("从成稿中心带着 signal_id 跳进来：诊断到了就选中同一条并高亮；找不到的 id 给一句提示", async () => {
    const { WriterRoom, WrDocs } = await loadWriter({ diagnosis: diagnosisPayload([ABSENCE, ECHO]) });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);

    await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: { posture: "deep", signal_id: ECHO.signal_id } })));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);
    // 平常进来不展开哪一条；带着 id 进来就停在那一条（哪怕它排在无抉择场景后面）
    const active = drawer.querySelector('.wr-dxd-row[aria-pressed="true"]');
    expect(active.textContent).toContain("贴邻重复");
    expect(host.querySelector('mark.wr-dx.is-active[data-dx="craft:adjacent_echo:11111111"]')).not.toBeNull();
    expect(drawer.textContent).toContain("整场"); // 缺席的发现没有位置，只列出
    expect(drawer.textContent).not.toContain("没有对应位置");

    // 深改姿态里再来一个当前作者稿没有的 id：提示，不崩
    await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: { posture: "deep", signal_id: "rules:model_voice:deadbeef" } })));
    await vi.waitFor(() => expect(drawer.textContent).toContain("没有对应位置"), T);
  });

  it("忽略 / 恢复按 signal_id 存到服务端（带修订号），成稿门读的就是这一份；重新诊断不丢忽略", async () => {
    const { WriterRoom, WrDocs, client } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);

    await click(drawer.querySelector(".wr-dxd-row"));
    await click(drawerButton(host, "忽略这一项"));
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/deep-review/preferences",
      expect.objectContaining({ ignored_issue_keys: ["craft:adjacent_echo:11111111"], base_revision_no: 0 }),
    ), T);
    expect(drawer.textContent).toContain("本场没有发现待改的句段");
    expect(drawer.textContent).toContain("已忽略 1 项");
    expect(host.querySelector("mark.wr-dx")).toBeNull();
    // 最近的决定折叠在底部：展开才看得到这一笔
    expect(drawer.textContent).not.toContain("忽略 · 贴邻叠句");
    await click(drawerButton(host, "最近的决定"));
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
    // 只有一个来源时不出来源下拉
    expect(sourceSelect(host)).toBeNull();

    await click(drawerButton(host, "开始深评"));
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/deep-review", {}), T);
    await vi.waitFor(() => expect(drawer.textContent).toContain("选择被说出来了"), T);
    expect(drawer.textContent).toContain("总分 58");
    // 刚跑完的这一轮：整体建议自己展开
    expect(drawer.textContent).toContain("把选择落成动作。");
    expect(drawerButton(host, "整体建议 1 条").getAttribute("aria-expanded")).toBe("true");
    expect(drawerButton(host, "重新深评")).not.toBeUndefined();
    expect(host.querySelector('mark.wr-dx[data-dx="ai:choice_pressure:99999999"]').textContent).toBe("能听见潮水");
    // 来源筛选：只看 AI
    expect([...sourceSelect(host).options].map((node) => node.textContent)).toEqual(["全部 2", "节奏 1", "AI 深评 1"]);
    await pickSource(host, "ai");
    expect(sourceSelect(host).value).toBe("ai");
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
    await click(drawerButton(host2, "开始深评"));
    await vi.waitFor(() => expect(host2.querySelector(".wr-dxd").textContent).toContain("没有可用的模型"), T);
    await click(drawerButton(host2, "去系统设置"));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("「AI 深评」对着空正文（409 WRITER_DEEP_REVIEW_NO_TEXT，不带 author_action）：照说服务端那句，不给「重试」也不叫去配置模型", async () => {
    const noText = Object.assign(new Error("这一场还没有正文，没有可评的字。先写一段（或起草一稿）再跑 AI 深评。"), { code: "WRITER_DEEP_REVIEW_NO_TEXT", status: 409, details: {} });
    const { WriterRoom, WrDocs } = await loadWriter({ aiRun: noText });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);
    await click(drawerButton(host, "开始深评"));
    await vi.waitFor(() => expect(drawer.textContent).toContain("这一场还没有正文，没有可评的字。"), T);
    const notice = drawer.querySelector(".wr-dxd-ai .wr-dxd-notice");
    expect([...notice.querySelectorAll("button")].map((node) => node.textContent.trim())).toEqual([]);
    expect(notice.textContent).not.toContain("系统设置");
  });

  it("「按诊断改写」：选中那一句回到起草，工具条按发现的改法直接出候选，改写请求带着发现的 id / 维度 / 改法", async () => {
    const { WriterRoom, WrDocs, client } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);
    await click(host.querySelector(".wr-dxd .wr-dxd-row"));

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

describe("写作台 · 1101–1280 宽时的深改（断点在文档刻度上，批准 #26c）", () => {
  it("上下文栏在这个宽度叠放；深改时诊断栏改为停靠、不画遮罩，回起草后又收回叠放", async () => {
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMediaAt(1200) });
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
    await click(host.querySelector(".wr-dxd .wr-dxd-row"));

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click(drawerButton(host, "选中去改"));
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


describe("写作台 · AI 看这一处（局部深评）", () => {
  it("复核一条发现：POST deep-review/passage 带 signal_id，意见挂在那条发现上；「按 AI 的判断忽略」走忽略", async () => {
    const opinion = { evaluation_id: "writer_passage_eval_1", paragraph_index: 0, about_signal_id: ECHO.signal_id, verdict: "does_not_hold", verdict_label: "不成立", assessment: "这是刻意的回响，不是毛病。", rewrite_brief: "", question: "", findings_count: 0, status: "current" };
    const passage = diagnosisPayload([{ ...ECHO, opinion }], { passage_reviews: [opinion], passage_review: opinion });
    const { WriterRoom, WrDocs, client } = await loadWriter({ passage });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);

    await click(drawer.querySelector(".wr-dxd-row"));
    await click(drawerButton(host, "AI 看这一处"));
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/deep-review/passage", { signal_id: ECHO.signal_id }), T);
    await vi.waitFor(() => expect(drawer.textContent).toContain("AI：不成立"), T);
    expect(drawer.textContent).toContain("这是刻意的回响，不是毛病。");
    // AI 判了不成立：忽略的按钮就说「按 AI 的判断忽略」，不再另有一个「忽略这一项」
    expect(drawerButton(host, "忽略这一项")).toBeUndefined();
    await click(drawerButton(host, "最近的决定"));
    expect(drawer.textContent).toContain("AI 看这一处 · 贴邻叠句");

    await click(drawerButton(host, "按 AI 的判断忽略"));
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/deep-review/preferences",
      expect.objectContaining({ ignored_issue_keys: ["craft:adjacent_echo:11111111"] }),
    ), T);
    expect(drawer.textContent).toContain("已忽略 1 项");
  });

  it("深改姿态里选中一段：「AI 看这一段」独立看这一段，结果与新发现并进面板", async () => {
    const note = { evaluation_id: "writer_passage_eval_2", paragraph_index: 0, about_signal_id: null, verdict: "no_finding", verdict_label: "没有要改的", assessment: "这一段的回响是节奏，不必动。", rewrite_brief: "", question: "", findings_count: 0, status: "current" };
    const passage = (body) => diagnosisPayload([ECHO], { passage_reviews: [note], passage_review: { ...note, paragraph_index: body.paragraph_index } });
    const { WriterRoom, WrDocs, client } = await loadWriter({ passage });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);

    await selectByOffsets(host.querySelector(".wr-editor"), 0, 5);
    const bar = document.querySelector(".wr-irw-bar");
    expect(bar).not.toBeNull();
    await click([...bar.querySelectorAll("button")].find((node) => node.textContent.includes("AI 看这一段")));
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/deep-review/passage", { paragraph_index: 0, excerpt: "门外很安静" }), T);
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("AI 看了第 1 段"), T);
    expect(host.querySelector(".wr-dxd").textContent).toContain("这一段的回响是节奏，不必动。");
    expect(host.querySelector(".wr-dxd").textContent).toContain("AI：没有要改的");
  });

  it("「AI 看这一段」看的是空段（409 WRITER_PASSAGE_REVIEW_NO_TEXT）：面板照说服务端那句，不给重试", async () => {
    const noText = Object.assign(new Error("要看的那一段是空的，没有可看的字。"), { code: "WRITER_PASSAGE_REVIEW_NO_TEXT", status: 409, details: {} });
    const { WriterRoom, WrDocs } = await loadWriter({ passage: noText });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);
    await selectByOffsets(host.querySelector(".wr-editor"), 0, 5);
    await click([...document.querySelector(".wr-irw-bar").querySelectorAll("button")].find((node) => node.textContent.includes("AI 看这一段")));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("要看的那一段是空的，没有可看的字。"), T);
    const notice = [...host.querySelectorAll(".wr-dxd .wr-dxd-notice")].find((node) => node.textContent.includes("要看的那一段是空的"));
    expect([...notice.querySelectorAll("button")]).toEqual([]);
  });

  it("选中跨两段的字：「AI 看这几段」按范围看（POST paragraph_start / paragraph_end），面板说「看了第 1–2 段」", async () => {
    const note = { evaluation_id: "writer_passage_eval_3", paragraph_index: 0, focus_paragraphs: [0, 1], paragraph_start: 0, paragraph_end: 1, whole_scene: true, about_signal_id: null, about_signal_ids: [], verdict: "no_finding", verdict_label: "没有要改的", assessment: "两段之间没有矛盾。", rewrite_brief: "", question: "", findings_count: 0, status: "current" };
    const passage = () => diagnosisPayload([ECHO], { passage_reviews: [note], passage_review: note });
    const { WriterRoom, WrDocs, client } = await loadWriter({ passage });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p><p>许望没有回答。录音里传来三声钟响。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("三声钟响"), T);
    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);

    /* 第 1 段第 10 个字起，选到第 2 段前 5 个字 */
    await selectByOffsets(host.querySelector(".wr-editor"), 10, 20);
    const bar = document.querySelector(".wr-irw-bar");
    expect(bar).not.toBeNull();
    const button = [...bar.querySelectorAll("button")].find((node) => node.textContent.includes("AI 看这几段"));
    expect(button).not.toBeUndefined();
    await click(button);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/scenes/s1/deep-review/passage", { paragraph_start: 0, paragraph_end: 1 }), T);
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("AI 看了第 1–2 段"), T);
    expect(host.querySelector(".wr-dxd").textContent).toContain("两段之间没有矛盾。");
    /* 范围的判断没给改法：既没有「改写这一段」，也没有「改写这几段」 */
    expect(drawerButton(host, "按这个改法改写这一段")).toBeUndefined();
    expect(drawerButton(host, "按这个改法改写这几段")).toBeUndefined();
  });

  it("看了第 1–2 段、给了改法：「按这个改法改写这几段」回到起草、选中这两段，按段送去改写（重评 R12）", async () => {
    const note = { evaluation_id: "writer_passage_eval_4", paragraph_index: 0, focus_paragraphs: [0, 1], paragraph_start: 0, paragraph_end: 1, whole_scene: true, about_signal_id: null, about_signal_ids: [], verdict: "partly", verdict_label: "部分成立", assessment: "两段的节奏贴得太近。", rewrite_brief: "把两段的节奏拉开。", question: "", findings_count: 0, status: "current" };
    const passage = () => diagnosisPayload([ECHO], { passage_reviews: [note], passage_review: note });
    const patch = { candidate: { patch_id: "p9", replacement_options: [{ option_id: "o1", paragraphs: ["门外安静了。", "许望没有回答，钟响了三声。"] }] } };
    const { WriterRoom, WrDocs, client } = await loadWriter({ passage, patch });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p><p>许望没有回答。录音里传来三声钟响。</p>");
    vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("三声钟响"), T);
    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);
    await selectByOffsets(host.querySelector(".wr-editor"), 10, 20);
    await click([...document.querySelector(".wr-irw-bar").querySelectorAll("button")].find((node) => node.textContent.includes("AI 看这几段")));
    await vi.waitFor(() => expect(drawerButton(host, "按这个改法改写这几段")).toBeTruthy(), T);
    expect(drawerButton(host, "按这个改法改写这一段")).toBeUndefined();

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click(drawerButton(host, "按这个改法改写这几段"));
      await act(async () => { vi.advanceTimersByTime(120); });
    } finally {
      vi.useRealTimers();
    }
    expect(host.querySelector(".wr-root").getAttribute("data-posture")).toBe("draft");
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/passages/patch-candidates", expect.objectContaining({
      source_excerpt: "门外很安静，安静到能听见潮水。\n许望没有回答。录音里传来三声钟响。",
      instruction: "把两段的节奏拉开。",
      quality_signal_id: "passage:writer_passage_eval_4",
      issue_dimension: "author_instruction",
    })), T);
    await vi.waitFor(() => expect(document.querySelector(".wr-irw-pop").textContent).toContain("替换为第 1 版"), T);
    expect(document.querySelector(".wr-irw-pop").textContent).toContain("按诊断：AI 看这几段");
    await click([...document.querySelectorAll(".wr-irw-pop button")].find((node) => node.textContent.trim().startsWith("替换为第 1 版")));
    const editor = host.querySelector(".wr-editor");
    expect([...editor.children].map((node) => node.textContent)).toEqual(["门外安静了。", "许望没有回答，钟响了三声。"]);
  });

  it("跨段的发现：行上标「与第 2 段矛盾」，展开给另一段的原话与「看第 2 段」；忽略后计数本地先记一笔", async () => {
    const CROSS = {
      ...AI_FINDING,
      signal_id: "ai:character_contradiction:dddd4444", quality_signal_id: "ai:character_contradiction:dddd4444",
      dimension: "character_contradiction", label: "人物自相矛盾",
      issue: "她「没有再看他」和前面等他回答的姿态矛盾。", recommendation: "让她在钟响之后才转开视线。",
      evidence: { excerpt: "安静到能听见潮水", paragraph_index: 0, start: 6, end: 14 },
      related: { excerpt: "三声钟响", paragraph_index: 1, start: 12, end: 16, kind: "contradiction", stale: false, label: "矛盾" },
      origin: { kind: "passage", evaluation_id: "writer_passage_eval_9", created_at: "2026-09-22T10:00:00Z", focus_paragraphs: [0], about_signal_id: null },
    };
    const { WriterRoom, WrDocs, WsDiagnosis } = await loadWriter({ diagnosis: diagnosisPayload([CROSS, ECHO]) });
    const summary = await import("./ws-diagnosis-summary.jsx");
    summary.WsDiagnosis.__reset();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p><p>许望没有回答。录音里传来三声钟响。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("三声钟响"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("与第 2 段矛盾"), T);
    /* 行上就标着「与第 2 段矛盾」；点开这一条：另一段的原话与跳转按钮，正文里另一段那句也标了 */
    expect(drawer.textContent).not.toContain("三声钟响");
    await click([...drawer.querySelectorAll(".wr-dxd-row")].find((node) => node.textContent.includes("没有再看他")));
    expect(drawer.textContent).toContain("三声钟响");
    expect(drawerButton(host, "看第 2 段")).not.toBeUndefined();
    await vi.waitFor(() => expect(host.querySelector(".wr-editor mark.wr-dx.is-related")).not.toBeNull(), T);
    expect(host.querySelector(".wr-editor mark.wr-dx.is-related").textContent).toBe("三声钟响");

    await click(drawerButton(host, "忽略这一项"));
    await vi.waitFor(() => expect(drawer.textContent).toContain("已忽略 1 项"), T);
    const counts = summary.WsDiagnosis.sceneCounts("s1");
    expect(counts).toMatchObject({ open: 1, ignored: 1 });
    void WsDiagnosis;
  });

  it("从对照检查带着 source=style 过来：清单筛到风格对照、停在它的第一条；那一块说检查的口径；就地重新检查，跑完重新取诊断", async () => {
    let gets = 0;
    const diagnosis = () => {
      gets += 1;
      return diagnosisPayload([ECHO, STYLE_FINDING], { style_bound: true, style: gets > 1 ? { ...STYLE_BLOCK, reading_id: "sfr-2" } : STYLE_BLOCK });
    };
    let posted = null;
    const check = (body) => {
      posted = body;
      return {
        job_id: "job-s", job: { job_id: "job-s", kind: "check", status: "succeeded", finished_at: "2026-10-20T06:05:00Z" },
        reading: { reading_id: "sfr-2", scene_id: "s1", project_id: "prj-main", judge: { overall: 6.6, samples: 3 } },
      };
    };
    const { WriterRoom, WrDocs } = await loadWriter({ diagnosis, check });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);

    await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: { posture: "deep", source: "style" } })));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("写到潮水时语调忽然庄重"), T);
    expect(sourceSelect(host).value).toBe("style");
    expect(drawer.textContent).not.toContain("贴邻重复。");
    expect(drawer.querySelector('.wr-dxd-row[aria-pressed="true"]').textContent).toContain("风格对照");
    expect(drawer.textContent).toContain("改法：在「能听见潮水」处，用一句自嘲压住抒情。");
    expect(host.querySelector('mark.wr-dx.is-active[data-dx="style:theme.emotional_tone:abcd1234"]').textContent).toBe("能听见潮水");
    const meta = drawer.querySelector('[data-testid="dx-style-meta"]').textContent;
    expect(meta).toContain("评审 6.4（6 分以上算像，已校准，3 次取中位）");
    // 常态（对着现在这份正文）不再写出来，只有改前时才多说一句
    expect(meta).not.toMatch(/改前|对着现在/);

    await click(drawer.querySelector('[data-testid="dx-style-check"]'));
    await vi.waitFor(() => expect(posted).toEqual({ scene_id: "s1" }), T);
    await vi.waitFor(() => expect(gets).toBe(2), T);
  });

  it("有参考绑定：面板说规则已按参考书校准、本场放过了哪些词；常态 / 常见的发现在行上标出来", async () => {
    const habit = { ...ABSENCE, severity: "info", house_taste: true, calibrated: { kind: "dimension_habit", level: "habit", share: 0.9, lower_bound: 0.82, n: 96 }, why: "参考作者的场里约 90% 也是这样（96 个窗口），只作提示。" };
    const common = { ...AI_FINDING, signal_id: "rules:model_voice:eeee5555", quality_signal_id: "rules:model_voice:eeee5555", source: "rules", dimension: "model_voice", label: "模型腔", lens: null, severity: "taste", house_taste: true, origin: null, calibrated: { kind: "dimension_habit", level: "common", share: 0.38, lower_bound: 0.3, n: 96 } };
    const diagnosis = diagnosisPayload([common, habit], {
      style_bound: true,
      craft_calibration: {
        source: "reference", book_title: "龙族", note: "按《龙族》校准：段落超过 194 字才提示；词表词按这位作者的密度判（每万字：看 41、手 30…），寻常用法不当毛病。",
        rules: { endings_source: "units", top_needles: [{ term: "看", per_10k: 41 }], habitual_dimensions: ["no_choice_scene"], common_dimensions: ["model_voice"] },
        waived_in_scene: [{ term: "看", count: 5, reference_per_10k: 41, expected: 9.8, probability: 0.96 }, { term: "光", count: 2, reference_per_10k: 12, expected: 2.9, probability: 0.78 }],
      },
    });
    const { WriterRoom, WrDocs } = await loadWriter({ diagnosis });
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("规则已按参考书校准 · 本场放过 2 个词"), T);
    // 校准的全文折叠着：一行说清楚，展开再看口径与放过了哪些词
    expect(drawer.textContent).not.toContain("按《龙族》校准");
    await click(drawerButton(host, "规则已按参考书校准"));
    expect(drawer.textContent).toContain("按《龙族》校准");
    expect(drawer.querySelector('[data-testid="dx-waived"]').textContent).toContain("本场按这位作者的密度放过 2 个词：看 ×5、光 ×2");
    expect(drawer.textContent).toContain("参考作者也常见");
    expect(drawer.textContent).toContain("参考作者的常态");
  });
});

describe("写作台 · 深改右栏的版面（2026-10-21 重排）", () => {
  async function openDeep(options) {
    const loaded = await loadWriter(options);
    vi.spyOn(loaded.WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<loaded.WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    await click(deepRadio(host));
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.querySelector(".wr-dxd-list, .wr-dxd-clear")).not.toBeNull(), T);
    return { ...loaded, host, drawer };
  }

  it("清单按严重程度分组、组内照服务端的次序；进来不展开哪一条；展开的卡只有一个主按钮；再点一下收起；忽略后停到下一条", async () => {
    // 服务端次序故意放反（审美在前）：面板照严重程度分组
    const { drawer } = await openDeep({ diagnosis: diagnosisPayload([ECHO, ABSENCE, AI_FINDING]) });
    expect([...drawer.querySelectorAll(".wr-dxd-group-h")].map((node) => node.textContent)).toEqual(["修订2", "审美1"]);
    expect([...drawer.querySelectorAll(".wr-dxd-t")].map((node) => node.textContent)).toEqual([
      ABSENCE.issue, AI_FINDING.issue, ECHO.issue,
    ]);
    const rowOf = (text) => [...drawer.querySelectorAll(".wr-dxd-row")].find((node) => node.textContent.includes(text));
    // 进来先看整张清单：哪一条都没展开
    expect(drawer.querySelector('.wr-dxd-row[aria-pressed="true"]')).toBeNull();
    expect(drawer.querySelector(".wr-dxd-detail")).toBeNull();
    // 每一行的说明：维度 · 来源 · 镜头（· 整场）
    expect(rowOf("看不到明确的抉择").querySelector(".wr-dxd-h").textContent).toBe("无抉择场景 · 规则 · 整场");
    expect(rowOf("选择被说出来了").querySelector(".wr-dxd-h").textContent).toBe("抉择压力 · AI 深评 · 故事");

    await click(rowOf("选择被说出来了"));
    expect(rowOf("选择被说出来了").getAttribute("aria-pressed")).toBe("true");
    const detail = drawer.querySelector(".wr-dxd-item.is-active .wr-dxd-detail");
    expect([...detail.querySelectorAll(".btn-accent")].map((node) => node.textContent.trim())).toEqual(["按诊断改写"]);
    expect([...detail.querySelectorAll("button")].map((node) => node.textContent.trim()))
      .toEqual(["为什么", "按诊断改写", "选中去改", "AI 看这一处", "忽略这一项"]);
    // 「为什么」折叠着
    expect(detail.textContent).not.toContain("读者要看见代价。");
    await click(drawerButton(drawer, "为什么"));
    expect(detail.textContent).toContain("读者要看见代价。");

    await click(rowOf("选择被说出来了"));
    expect(drawer.querySelector(".wr-dxd-detail")).toBeNull();
    expect(drawer.querySelector('.wr-dxd-row[aria-pressed="true"]')).toBeNull();

    // 正在一条条处理：忽略开着的那一条，就按清单次序停到下一条
    await click(rowOf("选择被说出来了"));
    await click(drawerButton(drawer, "忽略这一项"));
    await vi.waitFor(() => expect(drawer.textContent).toContain("已忽略 1 项"), T);
    expect(rowOf("看不到明确的抉择").getAttribute("aria-pressed")).toBe("true");
  });

  it("AI 复核过、认为成立并给了这一处的改法：主按钮就是「按 AI 的改法改写」，改写请求带 AI 的改法；没有第二个主按钮", async () => {
    const opinion = { evaluation_id: "writer_passage_eval_7", paragraph_index: 0, about_signal_id: ECHO.signal_id, verdict: "holds", verdict_label: "成立", assessment: "两个「安静」贴得太近。", rewrite_brief: "删掉第二个「安静」，改成「静得」。", question: "", findings_count: 0, status: "current" };
    const { host, drawer, client } = await openDeep({ diagnosis: diagnosisPayload([{ ...ECHO, opinion }]) });
    expect(drawer.querySelector(".wr-dxd-row .wr-dxd-badge").textContent).toBe("AI：成立");
    await click(drawer.querySelector(".wr-dxd-row"));
    const detail = drawer.querySelector(".wr-dxd-item.is-active .wr-dxd-detail");
    expect([...detail.querySelectorAll(".btn-accent")].map((node) => node.textContent.trim())).toEqual(["按 AI 的改法改写"]);
    expect(drawerButton(host, "按诊断改写")).toBeUndefined();

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click(drawerButton(host, "按 AI 的改法改写"));
      await act(async () => { vi.advanceTimersByTime(120); });
    } finally {
      vi.useRealTimers();
    }
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v1/passages/patch-candidates", expect.objectContaining({
      source_excerpt: "安静，安静",
      quality_signal_id: "craft:adjacent_echo:11111111",
      instruction: "删掉第二个「安静」，改成「静得」。",
    })), T);
  });

  it("已有的 AI 深评：一行说时间与总分，整体建议折叠着，点开才列出来", async () => {
    const ai = { status: "current", evaluation_id: "writer_deep_eval_5", overall_score: 0.58, revision_brief: [{ action: "把选择落成动作。" }, { action: "删掉解释动机的旁白。" }], lenses: [], llm_call_id: "c1", created_at: "2026-09-22T10:00:00Z" };
    const { host, drawer } = await openDeep({ diagnosis: diagnosisPayload([ECHO], { ai }) });
    const row = drawer.querySelector(".wr-dxd-ai");
    expect(row.querySelector(".wr-dxd-source-meta").textContent).toContain("总分 58");
    expect(drawerButton(host, "重新深评")).not.toBeUndefined();
    expect(drawer.textContent).not.toContain("把选择落成动作。");
    await click(drawerButton(host, "整体建议 2 条"));
    expect(drawer.textContent).toContain("把选择落成动作。");
    expect(drawer.textContent).toContain("删掉解释动机的旁白。");
  });

  it("「AI 看这一段」的结果放在清单上面，看完可以收起", async () => {
    const note = { evaluation_id: "writer_passage_eval_8", paragraph_index: 0, about_signal_id: null, verdict: "no_finding", verdict_label: "没有要改的", assessment: "这一段的回响是节奏，不必动。", rewrite_brief: "", question: "", findings_count: 0, status: "current" };
    const passage = (body) => diagnosisPayload([ECHO], { passage_reviews: [note], passage_review: { ...note, paragraph_index: body.paragraph_index } });
    const { host, drawer } = await openDeep({ passage });
    await selectByOffsets(host.querySelector(".wr-editor"), 0, 5);
    await click([...document.querySelector(".wr-irw-bar").querySelectorAll("button")].find((node) => node.textContent.includes("AI 看这一段")));
    await vi.waitFor(() => expect(drawer.textContent).toContain("AI 看了第 1 段"), T);
    const card = drawer.querySelector(".wr-dxd-passage");
    expect(card.compareDocumentPosition(drawer.querySelector(".wr-dxd-findings")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await click(card.querySelector('button[aria-label="收起这条结果"]'));
    expect(drawer.textContent).not.toContain("AI 看了第 1 段");
    expect(drawer.textContent).toContain("贴邻重复");
  });

  it("深改时右栏用诊断栏自己的宽度（默认 340），回起草还是上下文栏的；拖分隔线只改此刻这一栏、各记各的；底栏开关叫「诊断」", async () => {
    // 旧的存法只有 l / r：诊断栏取默认宽度，上下文栏照旧是作者拖过的 250
    window.localStorage.setItem("wr-rail", JSON.stringify({ l: 240, r: 250 }));
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    const dockRight = () => [...host.querySelectorAll(".wr-dock button")].find((node) => /上下文|诊断/.test(node.textContent));
    expect(railRight(host)).toBe("250px");
    expect(dockRight().textContent).toBe("上下文");

    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);
    expect(railRight(host)).toBe("340px");
    expect(dockRight().textContent).toBe("诊断");
    expect(dockRight().getAttribute("title")).toContain("深改诊断");

    // 右边的分隔线往左拖 60：诊断栏 400，上下文栏不动
    const handle = host.querySelector(".wr-resize-r");
    expect(handle.getAttribute("title")).toContain("诊断栏宽");
    await act(async () => {
      handle.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, clientX: 1000 }));
      window.dispatchEvent(new MouseEvent("pointermove", { clientX: 940 }));
      window.dispatchEvent(new MouseEvent("pointerup", { clientX: 940 }));
    });
    expect(railRight(host)).toBe("400px");
    expect(JSON.parse(window.localStorage.getItem("wr-rail"))).toEqual({ l: 240, r: 250, d: 400 });

    const draftRadio = [...host.querySelectorAll('[role="radio"]')].find((node) => node.textContent.includes("起草"));
    await click(draftRadio);
    expect(railRight(host)).toBe("250px");
    expect(dockRight().textContent).toBe("上下文");
  });
});
