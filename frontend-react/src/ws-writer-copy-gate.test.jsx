// 写作台 · 抄袭门拦下 AI 写出来的文字（2026-09-23 风格参考 v3 · P6b）：
// 后端唯一抄袭门拦下时回 409 SOURCE_SAFETY_BLOCKED，details.reference_copy 只有位置与计数（从不带参考原文），
// 还带一个 author_action——过去写作台看见 author_action 就当「没有可用的模型」，叫作者去系统设置。现在：
// · 每一版都被拦下（续写 / 选区改写）→ 说「其中一版的第 3–15 字与参考书原文连续相同，已丢掉，正文没动」，给「再试一次」；
// · 续写只丢了几版 → 候选上方说一句丢了几版；
// · 选区改写已经替换进正文、采纳时才被拦下 → 提示那一句的第几字要改。合成数据。
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
const DOCS = { ch01s1: "<p>门外的风很大，她没有回头。</p>" };

function copyBlocked({ subject = "这条改写", hits = [{ start: 2, end: 15 }], hitCount = hits.length, protectedHits = [] } = {}) {
  return Object.assign(new Error("reference copy gate blocked — raw english"), {
    code: "SOURCE_SAFETY_BLOCKED",
    status: 409,
    details: {
      reference_copy: {
        blocked: true, hit_count: hitCount, hits: hits.map((h) => ({ ...h, matched_chars: h.end - h.start, sha256: "x" })),
        protected_hit_count: protectedHits.length, protected_hits: protectedHits,
      },
      author_action: { title: `${subject}里有参考书的原文，不能进正文`, target_view: "writer", primary_button_label: "去改写这些位置" },
    },
  });
}

function matchMedia() {
  return { matches: false, media: "", addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
}

async function loadWriter() {
  const client = await import("./lib/client.js");
  installApiRouter(client, { catalog: [DEFAULT_CHAP] });
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBe(1), T);
  const store = await import("./wr-doc-store.jsx");
  const writer = await import("./ws-writer.jsx");
  const candidates = await import("./ws-writer-candidates.jsx");
  return { client, ...writer, ...store, ...candidates };
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
const startsWith = (root, text) => [...root.querySelectorAll("button")].find((node) => node.textContent.trim().startsWith(text));

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

describe("抄袭门的说法（纯函数）", () => {
  it("每一版都被拦下：kind copy，说哪一版的第几字、已丢掉、正文没动；不再当成「没有模型」", async () => {
    const { wrAiError } = await import("./ws-writer-ai.js");
    const info = wrAiError(copyBlocked({ hits: [{ start: 2, end: 15 }, { start: 30, end: 44 }], protectedHits: [{ start: 50, end: 52, term_sha256: "y", source: "protected_auto" }] }));
    expect(info.kind).toBe("copy");
    expect(info.actionLabel).toBe("再试一次");
    expect(info.offersSettings).toBeFalsy();
    expect(info.message).toBe("AI 给的几版都照搬了参考书原文（其中一版的第 3–15 字、第 31–44 字与参考书原文连续相同；第 51–52 字用了参考书里的专名（人名、地名等）），已经丢掉，正文没有改动。换个说法再试一次，或者自己写这一段。");
    expect(info.message).not.toContain("raw english");
    // 还是没配模型的 author_action 照旧去系统设置
    expect(wrAiError(Object.assign(new Error("x"), { code: "X", details: { author_action: { view: "settings" } } })).kind).toBe("config");
  });

  it("位置太多只说前三处与总数", async () => {
    const { copyGatePlaces } = await import("./ws-copy-gate.js");
    const hits = [0, 20, 40, 60].map((start) => ({ start, end: start + 12 }));
    expect(copyGatePlaces(copyBlocked({ hits, hitCount: 9 }))).toEqual(["第 1–12 字、第 21–32 字、第 41–52 字等 9 处与参考书原文连续相同"]);
  });
});

describe("写作台 · 选区改写", () => {
  it("每一版改写都照搬了参考书：弹层里说位置、已丢掉，给「再试一次」，不叫作者去系统设置", async () => {
    const { client, WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    vi.spyOn(WrDocs, "save").mockResolvedValue({});
    client.apiPost.mockImplementation((url) => (url === "/api/v1/passages/patch-candidates"
      ? Promise.reject(copyBlocked())
      : Promise.resolve({})));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    await select(host.querySelector(".wr-editor"), "风很大");
    await click(startsWith(document.querySelector(".wr-irw-bar"), "润色"));
    await vi.waitFor(() => expect(document.querySelector('[data-testid="wr-ai-error-copy"]')).not.toBeNull(), T);
    const block = document.querySelector('[data-testid="wr-ai-error-copy"]');
    expect(block.textContent).toContain("第 3–15 字与参考书原文连续相同");
    expect(block.textContent).toContain("正文没有改动");
    expect(block.textContent).not.toContain("去系统设置");
    expect(startsWith(block, "再试一次")).toBeTruthy();
  });

  it("替换进正文之后采纳才被拦下：提示那一句的第几字要改；别的采纳失败照旧不打扰", async () => {
    const { client, WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    let accept = () => Promise.reject(copyBlocked({ hits: [{ start: 0, end: 12 }] }));
    client.apiPost.mockImplementation((url) => {
      if (url === "/api/v1/passages/patch-candidates") {
        return Promise.resolve({ candidate: { patch_id: "p1", rationale: "ok", replacement_options: [{ option_id: "o1", replacement_text: "改写出来的一句" }] } });
      }
      if (url === "/api/v1/passage-patch-candidates/p1/accept") return accept();
      return Promise.resolve({});
    });
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    const editor = host.querySelector(".wr-editor");
    await select(editor, "风很大");
    await click(startsWith(document.querySelector(".wr-irw-bar"), "润色"));
    await vi.waitFor(() => expect(startsWith(document.body, "替换为第 1 版")).toBeTruthy(), T);
    await click(startsWith(document.body, "替换为第 1 版"));
    await vi.waitFor(() => expect(alert).toHaveBeenCalledTimes(1), T);
    expect(alert.mock.calls[0][0]).toBe("刚替换进正文的那句改写：第 1–12 字与参考书原文连续相同。把这几处改成你自己的说法——这次替换没有记成采纳，定稿时同样会被拦下。");
    expect(editor.textContent).toContain("改写出来的一句");

    // 不是抄袭门的失败（断网）：只影响偏好学习，不弹提示
    const { wrDecidePatch } = await import("./ws-writer-requests.js");
    accept = () => Promise.reject(Object.assign(new Error("down"), { code: "NETWORK_ERROR" }));
    await wrDecidePatch({ patchId: "p1", options: [{ option_id: "o1" }] }, 0, true);
    expect(alert).toHaveBeenCalledTimes(1);
    const onRefused = vi.fn();
    accept = () => Promise.reject(copyBlocked({ hits: [{ start: 4, end: 20 }] }));
    await wrDecidePatch({ patchId: "p1", options: [{ option_id: "o1" }] }, 0, true, { onRefused });
    expect(onRefused.mock.calls[0][0]).toContain("第 5–20 字与参考书原文连续相同");
    expect(alert).toHaveBeenCalledTimes(1);
  });
});

describe("写作台 · 续写", () => {
  it("只丢了一版：两条候选照给，上方说另有一版照搬了参考书、已丢掉", async () => {
    const { client, WrDocs, WrContinuePanel } = await loadWriter();
    vi.spyOn(WrDocs, "draftId").mockResolvedValue("draft-1");
    client.apiPost.mockImplementation((url) => (url === "/api/v1/author-drafts/draft-1/proposals/generate-set"
      ? Promise.resolve({
        proposals: [
          { proposal_id: "p1", proposal_source: "writer_room_continuation_variants:action", content: "她推开门。", rationale: "" },
          { proposal_id: "p2", proposal_source: "writer_room_continuation_variants:suspense", content: "门后没有人。", rationale: "" },
        ],
        reference_copy_blocked_count: 1,
      })
      : Promise.resolve({})));
    const host = await render(<WrContinuePanel sceneId="ch01s1" design={null} onAdopt={() => {}} onMerge={() => {}} onAdoptText={() => {}} />);
    await click(startsWith(host, "生成三条续写"));
    await vi.waitFor(() => expect(host.querySelector('[data-testid="wr-cand-copy-note"]')).not.toBeNull(), T);
    expect(host.querySelector('[data-testid="wr-cand-copy-note"]').textContent).toBe("另有 1 版与参考书原文连续相同（或用了它的专名），已经丢掉。");
    expect(host.querySelectorAll(".wr-cand").length).toBe(2);
  });

  it("三版都被拦下：说成抄袭门拦下，不说没有模型", async () => {
    const { client, WrDocs, WrContinuePanel } = await loadWriter();
    vi.spyOn(WrDocs, "draftId").mockResolvedValue("draft-1");
    client.apiPost.mockImplementation((url) => (url === "/api/v1/author-drafts/draft-1/proposals/generate-set"
      ? Promise.reject(copyBlocked({ subject: "这条 AI 建议", hits: [{ start: 10, end: 24 }] }))
      : Promise.resolve({})));
    const host = await render(<WrContinuePanel sceneId="ch01s1" design={null} onAdopt={() => {}} onMerge={() => {}} onAdoptText={() => {}} onOpenSettings={() => {}} />);
    await click(startsWith(host, "生成三条续写"));
    await vi.waitFor(() => expect(host.querySelector('[data-testid="wr-ai-error-copy"]')).not.toBeNull(), T);
    expect(host.textContent).toContain("第 11–24 字与参考书原文连续相同");
    expect(host.textContent).not.toContain("没有可用的模型");
    expect(startsWith(host, "去系统设置")).toBeUndefined();
  });
});
