// 风格参考页（参考书 → 学习文风 → 用于作品）的界面：书库多选删除、导入对话框（默认范围来自 /runtime、重复导入
// →「打开这本」）、段落分类（旧版启发式提醒 + 先报费用再确认的「用模型重新分类」）、学习文风（估算 / 进度 / 取消 /
// 建议重学）、文风画像（气质、✓ / ✗、原话、逐维「重点 / 正常 / 不学」）、用于作品（三种参考方式、样例窗数与实测字数、
// 直接用于 / 解除）、本场预览。旧界面的四种策略、强度、任务类型、👍/👎 都不该再出现。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  getOperatorRef: vi.fn(() => "operator"),
}));
const workHolder = { current: { id: "w1", title: "北岸手记" } };
vi.mock("./ws-works.jsx", () => ({
  WsWorks: {
    active: () => workHolder.current,
    activeId: () => (workHolder.current ? workHolder.current.id : null),
  },
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const API = "/api/v2/style-reference";

const client = await import("./lib/client.js");
const store = await import("./ws-styleref-store.js");
const fidStore = await import("./ws-fidelity-store.js");
const { WsStyleRef } = await import("./ws-styleref.jsx");

/* ---------- 合成数据（中性占位，不是任何真书） ---------- */
function bookRow(over = {}) {
  return {
    book_id: "bk-a", title: "甲书", author_label: "某甲", total_chars: 48000, status: "ready", cloud_policy: "allow_full_cloud",
    paragraph_count: 900, paragraph_types_revision: 2, classification_provenance: { source: "llm", llm_paragraphs: 900, agreement: 0.91 },
    classification: null, learn: null, profile: null, profile_count: 0, applied_projects: [], created_at: "2026-09-20T08:00:00Z",
    ...over,
  };
}
const PROFILE_SUMMARY = { profile_id: "pf-a", title: "甲书 · 文风", status: "active", profile_version: "style_profile_v3", learned_at: "2026-09-21T10:00:00Z", card_lines: 3, needs_relearn: false, relearn_reason: null };

const PROFILE_DETAIL = {
  profile_id: "pf-a", book_id: "bk-a", title: "甲书 · 文风", has_card: true, needs_relearn: false, relearn_reason: null,
  learned_from: { learned_at: "2026-09-21T10:00:00Z" }, version_tag: "v3",
  temperament: ["冷眼旁观，不替人物下结论"], qualitative_summary: "克制、短句多。",
  card_line_states: {},
  dimensions: [
    {
      dimension: "scene.dialogue", layer: "scene", label: "对话写法", summary: "对白短，一问一答", model_default: "对白里交代来龙去脉",
      devices: ["留白"], quote_count: 2, evidence_count: 1,
      lines: [
        {
          line_id: "L1", text: "对白常常只有半句", kind: "do", mandatory: true, state: null, evidence_count: 2,
          evidence: [{ quote_id: "q1", text: "“去吗？”“再说。”", paragraph_index: 3 }],
        },
        { line_id: "L2", text: "不在对白里解释动机", kind: "avoid", mandatory: false, state: null, evidence_count: 0, evidence: [] },
      ],
    },
    {
      dimension: "language.sentence_structure", layer: "language", label: "句式结构", summary: "短句为主", model_default: "长短句均匀",
      devices: [], quote_count: 0, evidence_count: 0,
      lines: [{ line_id: "L3", text: "动作句多以动词收尾", kind: "do", mandatory: false, state: null, evidence_count: 0, evidence: [] }],
    },
  ],
  voice: { habits: ["句尾少用语气词"] },
  structure: { lines: ["每章约 3 场"] },
};

const OWN_BINDING = {
  binding_id: "bd-1", profile_id: "pf-a", scope: "project", scope_ref_id: "w1", status: "active",
  config: { reference_mode: "full", sample_windows: 12, draft_mode: "style_first", dimension_states: {} },
};

const CATALOG = {
  chapters: [
    { chapter_id: "c1", no: 1, title: "雾里", scenes: [{ scene_id: "sc-1", title: "码头" }, { scene_id: "sc-2", title: "夜渡" }] },
  ],
};

const PREVIEW = {
  reference_mode: "full", requested_reference_mode: "full", sample_windows: 12,
  scene: { scene_id: "sc-1", found: true, position: "opening", situation_tags: ["开章引入"] },
  windows: [
    {
      window_no: 7, chapter: 3, position: "opening", start: 120, end: 150, chars: 3900, paragraphs: 31, slot: "position",
      situations: ["开章引入"], moods: ["平静"], devices: ["留白"], gist: "某人在渡口等船", paragraph_type: "narration",
    },
  ],
  blocks: { card: "[文风卡]\n· 对白常常只有半句", voice: "[声音]\n· 句尾少用语气词", samples: "……", red_line: "[红线]\n不许照搬" },
  sizes: { sample_windows: 12, sample_chars: 48000, card_chars: 20, card_lines: 3, total_chars: 51000 },
  notices: [],
};

/* ---------- 路由 ---------- */
let state;

function resetState() {
  state = {
    books: [bookRow()],
    runtime: { llm_enabled: true, llm_is_local: false, default_cloud_policy: "allow_full_cloud" },
    projectBinding: { project_id: "w1", binding: null, profile: null, book: null },
    profileBindings: [],
    activity: [],
    learn: { learn: null, estimate: { est_calls: 12, calls: { extract: 4, tags: 6 }, est_input_chars: { extract_per_call: 44000 } } },
    estimate: { est_calls: 120, est_input_tokens: 350000, est_output_tokens: 9000, est_minutes: 75, parallel: 3 },
    profile: JSON.parse(JSON.stringify(PROFILE_DETAIL)),
  };
}

function installRoutes() {
  client.apiGet.mockImplementation((url) => {
    const path = url.split("?")[0];
    if (path === `${API}/books`) return Promise.resolve({ books: state.books });
    if (path === `${API}/runtime`) return Promise.resolve(state.runtime);
    if (path === `${API}/activity`) return Promise.resolve({ items: state.activity });
    let m = path.match(/^\/api\/v2\/style-reference\/books\/([^/]+)$/);
    if (m) return Promise.resolve({ book: { ...state.books.find((b) => b.book_id === m[1]), stats_json: { paragraph_type_distribution: { narration: 0.6, dialogue: 0.4 } } } });
    m = path.match(/^\/api\/v2\/style-reference\/books\/([^/]+)\/learn$/);
    if (m) return Promise.resolve(state.learn);
    m = path.match(/^\/api\/v2\/style-reference\/books\/([^/]+)\/classification\/estimate$/);
    if (m) return Promise.resolve({ estimate: state.estimate });
    m = path.match(/^\/api\/v2\/style-reference\/books\/([^/]+)\/paragraphs$/);
    if (m) return Promise.resolve({ paragraphs: [{ paragraph_index: 120, text: "渡口的灯一盏一盏灭了。" }] });
    if (path === `${API}/profiles/pf-a`) return Promise.resolve({ profile: state.profile });
    if (path === `${API}/profiles/pf-a/bindings`) return Promise.resolve({ bindings: state.profileBindings });
    if (path === `${API}/profiles/pf-a/banned-terms`) return Promise.resolve({ terms: [{ term_id: "t1", term: "某地", source: "protected_auto", scope: "generation" }] });
    if (path === `${API}/projects/w1/style-binding`) return Promise.resolve(state.projectBinding);
    if (path === "/api/v2/projects/w1/catalog") return Promise.resolve(CATALOG);
    return Promise.resolve({});
  });
  client.apiPost.mockImplementation((url) => {
    if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
    return Promise.resolve({});
  });
  client.apiPatch.mockResolvedValue({});
  client.apiDelete.mockResolvedValue({});
}

/* ---------- 挂载 ---------- */
const mounted = [];
async function mount(element) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(element); });
  mounted.push({ root, host });
  return host;
}
async function settle(ms = 0) {
  await act(async () => { await new Promise((r) => setTimeout(r, ms)); });
}
async function click(el) {
  expect(el, "要点的元素不存在").toBeTruthy();
  await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
}
const $ = (sel, root = document.body) => root.querySelector(sel);
const $$ = (sel, root = document.body) => Array.from(root.querySelectorAll(sel));
const byTestId = (id) => $(`[data-testid="${id}"]`);
async function openStage(id) {
  await click($(`.sr-step[data-stage="${id}"]`));
  await settle();
}
async function mountView(go = vi.fn()) {
  const host = await mount(<WsStyleRef go={go} />);
  await settle();
  await settle();
  return host;
}

beforeEach(() => {
  resetState();
  workHolder.current = { id: "w1", title: "北岸手记" };
  client.apiGet.mockReset();
  client.apiPost.mockReset();
  client.apiPatch.mockReset();
  client.apiDelete.mockReset();
  installRoutes();
  store.srResetForTests();
  fidStore.fidResetForTests();
  try { localStorage.clear(); } catch (e) { /* ignore */ }
  // 提示层没挂（单独渲染这一页）时 srNotify 退回 window.alert；jsdom 没实现它
  vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  store.srResetForTests();
  fidStore.fidResetForTests();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("页面外壳", () => {
  it("三步：参考书 → 学习文风 → 用于作品，外加随时可用的「对照检查」；没学过的书落在「学习文风」；旧界面的叫法都不在", async () => {
    await mountView();
    expect($$(".sr-step").map((b) => b.dataset.stage)).toEqual(["book", "learn", "apply", "check"]);
    expect($('.sr-step[data-stage="check"]').getAttribute("aria-label")).toBe("对照检查（等前一步）");
    expect($(".sr-step.is-active").dataset.stage).toBe("learn");
    const text = document.body.textContent;
    for (const gone of ["维度矩阵", "注入", "回测", "强度", "MIXED", "策略 A", "任务类型", "👍", "👎", "前 200 段", "锚定集"]) {
      expect(text, gone).not.toContain(gone);
    }
  });

  it("空书库：给「导入第一本参考书」", async () => {
    state.books = [];
    await mountView();
    expect(byTestId("sr-import-first")).toBeTruthy();
  });
});

describe("参考书库：多选删除", () => {
  it("选两本 → 「删除所选」→ 确认框列出书名 → 批量删除", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书", author_label: "" })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => {
      if (url === `${API}/books/bulk-delete`) {
        return Promise.resolve({ results: [{ book_id: "bk-a", deleted: true }, { book_id: "bk-b", deleted: true }], deleted_count: 2, failed_count: 0 });
      }
      return Promise.resolve({});
    });
    await mountView();
    await click(byTestId("sr-books-select"));
    const boxes = $$("[data-sr-select]");
    expect(boxes.map((b) => b.dataset.srSelect)).toEqual(["bk-a", "bk-b"]);
    expect(byTestId("sr-books-delete-selected").disabled).toBe(true);
    for (const box of boxes) await click(box);
    expect($(".sr-select-all b").textContent).toBe("2");
    state.books = [];
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(confirm).toHaveBeenCalledTimes(1);
    const message = confirm.mock.calls[0][0];
    expect(message).toContain("删除 2 本参考书？");
    expect(message).toContain("《甲书》、《乙书》");
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bulk-delete`, { book_ids: ["bk-a", "bk-b"] });
    expect(byTestId("sr-select-bar")).toBeNull();
  });

  it("确认框点取消：不发删除请求", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($("[data-sr-select]"));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/bulk-delete"))).toBe(false);
    expect($$("[data-sr-select]")).toHaveLength(1);
  });

  it("正在用于当前作品的书，确认框里点明", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: [{ project_id: "w1", binding_id: "bd-1", config: {} }] })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($("[data-sr-select]"));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("《北岸手记》正在用《甲书》的文风");
  });
});

describe("导入对话框", () => {
  async function openImport() {
    await click(byTestId("sr-books-import"));
    await settle();
  }

  it("默认范围跟着 /runtime；三档说法如实；非本机要两项权属声明", async () => {
    state.runtime = { llm_enabled: true, llm_is_local: false, default_cloud_policy: "allow_full_cloud" };
    await mountView();
    await openImport();
    const radios = $$('input[name="sr-cloud-policy"]');
    expect(radios.map((r) => r.value)).toEqual(["local_only", "segments_only", "allow_full_cloud"]);
    expect(radios.find((r) => r.checked).value).toBe("allow_full_cloud");
    const policyText = byTestId("sr-import-policy").textContent;
    expect(policyText).toContain("仅本机模型");
    expect(policyText).toContain("只发短句起草时只用文风卡，不发原文");
    expect(policyText).toContain("可发送全文");
    expect(policyText).toContain("按当前模型推荐");
    expect(byTestId("sr-rights-send")).toBeTruthy();
    expect(byTestId("sr-import-submit").disabled).toBe(true);
    expect(byTestId("sr-import-why").textContent).toBe("先确认权属声明");
  });

  it("没有模型：说明并锁住「导入」", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView();
    await openImport();
    expect(byTestId("sr-import-no-llm")).toBeTruthy();
    expect($('input[name="sr-cloud-policy"]:checked').value).toBe("local_only");
    expect(byTestId("sr-import-why").textContent).toContain("先接入模型");
    expect(byTestId("sr-rights-send")).toBeNull();
    // 没有模型就谈不上「按当前模型推荐」
    expect(byTestId("sr-import-policy").textContent).not.toContain("按当前模型推荐");
  });

  it("选文件自动填书名，导入发 FormData；同一份文本 → 说出书名并给「打开这本」", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书" })];
    await mountView();
    await openImport();
    await click(byTestId("sr-rights-analysis"));
    await click(byTestId("sr-rights-send"));
    const input = byTestId("sr-import-file");
    const file = new File(["第一章\n一段正文。"], "丙书.txt", { type: "text/plain" });
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(byTestId("sr-import-title").value).toBe("丙书");
    client.apiPost.mockImplementation((url) => {
      if (url === `${API}/books/import-upload`) {
        return Promise.reject(Object.assign(new Error("duplicate"), {
          code: "STYLE_REFERENCE_BOOK_DUPLICATE", details: { book_id: "bk-b", title: "乙书" },
        }));
      }
      return Promise.resolve({});
    });
    await click(byTestId("sr-import-submit"));
    await settle();
    const [url, body] = client.apiPost.mock.calls.find(([u]) => u.endsWith("/import-upload"));
    expect(url).toBe(`${API}/books/import-upload`);
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("cloud_policy")).toBe("allow_full_cloud");
    expect(JSON.parse(body.get("rights_declaration"))).toMatchObject({ analysis_rights: true, send_rights: true });
    expect(byTestId("sr-import-error").textContent).toContain("书库里已经有同一份文本：《乙书》。");
    await click(byTestId("sr-import-error-action"));
    await settle();
    expect($(".sr-stage-title").textContent).toBe("乙书");
    expect(byTestId("sr-import-submit")).toBeNull();
  });
});

describe("第一步 · 参考书", () => {
  it("旧版启发式标的类型：提醒并说出一致率；「用模型重新分类（保留画像）」先报费用再确认", async () => {
    state.books = [bookRow({ classification_provenance: { source: "legacy_heuristic", agreement: 0.42, heuristic_paragraphs: 700, llm_paragraphs: 200 } })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/reclassify") ? { job_id: "job-r", mode: "retype" } : {}));
    await mountView();
    await openStage("book");
    const notice = byTestId("sr-overview-legacy-types");
    expect(notice.textContent).toContain("700 段是旧版导入时用启发式规则标的类型");
    expect(notice.textContent).toContain("只有 42% 一致");
    await click(byTestId("sr-overview-retype"));
    await settle();
    expect(client.apiGet).toHaveBeenCalledWith(`${API}/books/bk-a/classification/estimate`);
    expect(confirm.mock.calls[0][0]).toContain("约 120 次模型调用");
    expect(confirm.mock.calls[0][0]).toContain("文风画像和用在作品上的设置都保留");
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/reclassify`, { mode: "retype" });
  });

  it("确认框取消：不重新分类", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await openStage("book");
    await click(byTestId("sr-overview-retype"));
    await settle();
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/reclassify"))).toBe(false);
  });

  it("没有模型：「用模型重新分类」锁住并说明", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView();
    await openStage("book");
    expect(byTestId("sr-overview-retype").disabled).toBe(true);
    expect(byTestId("sr-overview-model-gate").textContent).toContain("还没有接入模型");
  });

  it("分类没完成：给「继续分类」", async () => {
    state.books = [bookRow({ status: "failed", classification: { batches_done: 3, batches_total: 10, error: { code: "STYLE_REFERENCE_LLM_REQUIRED" } } })];
    await mountView();
    expect($(".sr-step.is-active").dataset.stage).toBe("book");
    expect(byTestId("sr-overview-classify").textContent).toContain("已分好 3/10 批");
    client.apiPost.mockResolvedValueOnce({ job_id: "job-c", mode: "reclassify" });
    await click(byTestId("sr-overview-resume"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/reclassify`, { resume: true });
  });
});

describe("第二步 · 学习文风", () => {
  it("没学过：按钮 + 估算；开始后显示进度，可取消", async () => {
    await mountView();
    expect(byTestId("sr-learn-status").textContent).toBe("还没学");
    expect(byTestId("sr-learn-estimate").textContent).toContain("约 12 次模型调用");
    expect(byTestId("sr-portrait-empty")).toBeTruthy();
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "job-l" } : {}));
    state.activity = [{ key: "job:job-l", job_id: "job-l", kind: "learn", status: "running", book_id: "bk-a", percent: 30, phase_label: "学习文风 · 分层读原文", steps: { done: 1, total: 4 } }];
    await click(byTestId("sr-learn-start"));
    await settle(20);
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, {});
    expect(byTestId("sr-learn-running")).toBeTruthy();
    expect(byTestId("sr-learn-running").textContent).toContain("分层读原文 1/4");
    expect(byTestId("sr-learn-status").textContent).toBe("学习中 30%");
    await click(byTestId("sr-learn-cancel"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn/cancel`, {});
  });

  it("没有模型：错误说成中文，并给「去设置模型」", async () => {
    const go = vi.fn();
    client.apiPost.mockImplementation((url) => (url.endsWith("/learn")
      ? Promise.reject(Object.assign(new Error("llm required"), { code: "STYLE_REFERENCE_LLM_REQUIRED", details: { author_action: { view: "systemConfig" } } }))
      : Promise.resolve({})));
    await mountView(go);
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(byTestId("sr-learn-error").textContent).toContain("这一步要用模型，但还没有接入可用的模型。");
    expect(byTestId("sr-learn-error").textContent).not.toContain("llm required");
    await click(byTestId("sr-learn-error-action"));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("先看得到的拦路：没有模型时「学习文风」锁住并给「去设置模型」，不白发一次请求", async () => {
    const go = vi.fn();
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView(go);
    expect(byTestId("sr-learn-no-llm").textContent).toContain("还没有接入模型");
    expect(byTestId("sr-learn-start").disabled).toBe(true);
    await click($('[data-testid="sr-learn-no-llm"] button'));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/learn"))).toBe(false);
  });

  it("「仅本机模型」的书、学习节点在云端：说清楚并锁住", async () => {
    state.books = [bookRow({ cloud_policy: "local_only" })];
    state.learn = { ...state.learn, routes: [{ node_id: "style_ref_extract_language", local: false }, { node_id: "style_ref_tag_windows", local: true }] };
    await mountView();
    expect(byTestId("sr-learn-cloud-blocked").textContent).toContain("学习用的模型不在本机");
    expect(byTestId("sr-learn-start").disabled).toBe(true);
  });

  it("段落类型更新过：建议重新学习；重新学习先确认", async () => {
    state.books = [bookRow({ profile: { ...PROFILE_SUMMARY, needs_relearn: true, relearn_reason: "types_changed" } })];
    state.profile = { ...state.profile, needs_relearn: true, relearn_reason: "types_changed" };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    expect($(".sr-step.is-active").dataset.stage).toBe("learn");
    expect(byTestId("sr-learn-relearn").textContent).toContain("段落类型已更新，建议重新学习");
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("重新学习《甲书》的文风？");
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/learn"))).toBe(false);
  });
});

describe("文风画像", () => {
  async function openPortrait({ applied = false } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: applied ? [{ project_id: "w1", binding_id: "bd-1", config: OWN_BINDING.config }] : [] })];
    if (applied) state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    await mountView();
    await openStage("learn");
    await settle();
  }
  const dimRow = (dim) => $(`.sr-dim[data-dimension="${dim}"]`);

  it("气质在最前；16 维按层分组；展开看通用写法对这位作者、原话", async () => {
    await openPortrait();
    expect(byTestId("sr-temperament").textContent).toContain("冷眼旁观，不替人物下结论");
    expect($$(".sr-dim-group-title").map((h) => h.textContent)).toEqual(["语言层", "场景层"]);
    await click($(".sr-dim-toggle", dimRow("scene.dialogue")));
    const row = dimRow("scene.dialogue");
    expect(row.textContent).toContain("通用写法对白里交代来龙去脉");
    expect(row.textContent).toContain("这位作者");
    expect(row.textContent).toContain("作者不这么写");
    expect(row.textContent).toContain("必须体现");
    await click($(".sr-line-quotes-toggle", row));
    expect($(".sr-quote", row).textContent).toContain("第 4 段");
    expect($(".sr-quote-more", row).textContent).toBe("另有 1 条");
  });

  it("✓ 总带上：先标上再等服务端；服务端拒绝就回滚", async () => {
    await openPortrait();
    await click($(".sr-dim-toggle", dimRow("scene.dialogue")));
    const line = () => $('[data-line-id="L1"]');
    let release;
    client.apiPost.mockImplementation((url) => (url.includes("/card-lines/")
      ? new Promise((resolve, reject) => { release = { resolve, reject }; })
      : Promise.resolve({})));
    await click($('[data-testid="sr-line-pinned"]', line()));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/card-lines/L1`, { state: "pinned" });
    expect(line().className).toContain("is-pinned");
    await act(async () => { release.reject(Object.assign(new Error("x"), { code: "STYLE_REFERENCE_CARD_LINE_NOT_FOUND" })); });
    await settle();
    expect(line().className).not.toContain("is-pinned");
    expect(window.alert).toHaveBeenCalledWith("这一句已经不在文风卡上了（可能刚重新学过）。");
  });

  it("没用于当前作品：逐维状态锁住并说明；用上之后改「重点」写在绑定上", async () => {
    await openPortrait();
    expect(byTestId("sr-states-locked").textContent).toContain("这本书还没用于《北岸手记》");
    expect(byTestId("sr-dim-state-scene.dialogue-emphasize")).toBeNull();
    for (const { root, host } of mounted.splice(0)) { await act(async () => root.unmount()); host.remove(); }
    store.srResetForTests();
    await openPortrait({ applied: true });
    expect(byTestId("sr-states-hint").textContent).toContain("给《北岸手记》设的");
    client.apiPatch.mockResolvedValueOnce({ binding: { ...OWN_BINDING, config: { ...OWN_BINDING.config, dimension_states: { "scene.dialogue": "emphasize" } } }, changed: true });
    await click(byTestId("sr-dim-state-scene.dialogue-emphasize"));
    await settle();
    expect(client.apiPatch).toHaveBeenCalledWith(`${API}/bindings/bd-1`, { config: { dimension_states: { "scene.dialogue": "emphasize" } } });
    expect(byTestId("sr-dim-state-scene.dialogue-emphasize").getAttribute("aria-checked")).toBe("true");
  });

  it("旧版画像：说明并列出起草时读的句子，含数字的标「起草时不带」", async () => {
    state.profile = {
      profile_id: "pf-a", has_card: false, needs_relearn: true, relearn_reason: "legacy_profile", dimensions: [], temperament: [],
      legacy: { summary: "旧摘要", groups: [{ key: "style_features", label: "写法特征", lines: [{ text: "短句多", dropped_in_drafting: false }, { text: "平均句长 12 字", dropped_in_drafting: true }] }] },
      voice: { habits: [] }, structure: { lines: [] },
    };
    await openPortrait();
    const legacy = byTestId("sr-portrait-legacy");
    expect(legacy.textContent).toContain("这是旧版画像");
    expect(legacy.textContent).toContain("起草时不带");
  });
});

describe("第三步 · 用于作品", () => {
  async function openApply({ own = false, bookOver = {} } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, ...bookOver })];
    if (own) state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    await mountView();
    await openStage("apply");
    await settle();
  }

  it("三种参考方式、样例窗数（旁边是实测字数）、起草方式；没有旧的策略 / 强度 / 开关", async () => {
    await openApply();
    expect($$('[data-testid="sr-reference-mode"] [role="radio"]').map((b) => b.dataset.value)).toEqual(["full", "samples_only", "card_only"]);
    expect($$('[data-testid="sr-draft-mode"] [role="radio"]').map((b) => b.dataset.value)).toEqual(["style_first", "neutral_first"]);
    const slider = byTestId("sr-sample-windows");
    expect(slider.min).toBe("0");
    expect(slider.max).toBe("16");
    expect(slider.value).toBe("12");
    await settle(400);
    expect(byTestId("sr-sample-readout").textContent).toContain("约带 12 窗、4.8 万字原文");
    const previewCall = client.apiPost.mock.calls.find(([url]) => url.endsWith("/injection-preview"));
    expect(previewCall[1]).toMatchObject({ reference_mode: "full", sample_windows: 12 });
    const text = byTestId("sr-apply-form").textContent;
    for (const gone of ["强度", "策略", "任务类型", "全局"]) expect(text, gone).not.toContain(gone);
  });

  it("只用文风卡：窗数锁住并说明不发原文", async () => {
    await openApply();
    await click($('[data-testid="sr-reference-mode"] [data-value="card_only"]'));
    expect(byTestId("sr-sample-windows").disabled).toBe(true);
    expect(byTestId("sr-sample-readout").textContent).toBe("只用文风卡：起草时不发原文段落。");
  });

  it("「只发短句」的书：带原文的两种方式用不了", async () => {
    await openApply({ bookOver: { cloud_policy: "segments_only" } });
    expect(byTestId("sr-apply-segments-only")).toBeTruthy();
    expect($('[data-testid="sr-reference-mode"] [data-value="full"]').disabled).toBe(true);
    expect($('[data-testid="sr-reference-mode"] [data-value="card_only"]').getAttribute("aria-checked")).toBe("true");
  });

  it("直接用于当前作品，结果就地说明", async () => {
    await openApply();
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) return Promise.resolve({ binding: OWN_BINDING, created: true, changed: true, replaced: [] });
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    const submit = byTestId("sr-apply-submit");
    expect(submit.textContent).toContain("用于《北岸手记》");
    await click(submit);
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/apply`, {
      scope: "project", scope_ref_id: "w1", config: { reference_mode: "full", sample_windows: 12, draft_mode: "style_first" },
    });
    expect(byTestId("sr-apply-result").textContent).toContain("已用于《北岸手记》");
  });

  it("已在用：改了窗数才能「保存设置」；解除先确认再删", async () => {
    await openApply({ own: true });
    expect(byTestId("sr-apply-status").textContent).toContain("《北岸手记》正在用这本书的文风：全面模仿 · 12 窗 · 作者手笔直起");
    expect(byTestId("sr-apply-submit").disabled).toBe(true);
    const slider = byTestId("sr-sample-windows");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(slider, "4");
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(byTestId("sr-apply-submit").textContent).toContain("保存设置");
    client.apiPatch.mockResolvedValueOnce({ binding: { ...OWN_BINDING, config: { ...OWN_BINDING.config, sample_windows: 4 } }, changed: true });
    await click(byTestId("sr-apply-submit"));
    await settle();
    expect(client.apiPatch).toHaveBeenCalledWith(`${API}/bindings/bd-1`, { config: { reference_mode: "full", sample_windows: 4, draft_mode: "style_first" } });
    expect(byTestId("sr-apply-result").textContent).toContain("已保存");

    vi.spyOn(window, "confirm").mockReturnValue(true);
    await click(byTestId("sr-apply-unbind"));
    await settle();
    expect(client.apiDelete).toHaveBeenCalledWith(`${API}/bindings/bd-1`);
  });

  it("用着别的书：说清用这本会替换它", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = {
      project_id: "w1", binding: { ...OWN_BINDING, binding_id: "bd-x", profile_id: "pf-x" }, profile: { profile_id: "pf-x" }, book: { book_id: "bk-x", title: "丁书" },
    };
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-other").textContent).toContain("现在用的是《丁书》的文风；用这本会替换它");
    expect(byTestId("sr-apply-submit").textContent).toContain("改用这本");
  });

  it("本场预览：挑一场，看样例窗（第几章 · 章首 / 章末、梗概、标签）和文风卡", async () => {
    await openApply();
    await settle();
    const select = byTestId("sr-scene-select");
    expect($$("option", select).map((o) => o.textContent)).toContain("第 1 章 · 第 2 场「夜渡」");
    await act(async () => {
      select.value = "sc-1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    client.apiPost.mockClear();
    await click(byTestId("sr-scene-preview-run"));
    await settle();
    const [url, body] = client.apiPost.mock.calls.find(([u]) => u.endsWith("/injection-preview"));
    expect(url).toBe(`${API}/profiles/pf-a/injection-preview`);
    expect(body).toMatchObject({ scene_id: "sc-1", project_id: "w1", sample_windows: 12 });
    const result = byTestId("sr-scene-preview-result");
    expect(result.textContent).toContain("开章的一场 · 开章引入");
    const win = $(".sr-window", result);
    expect(win.textContent).toContain("第 3 章 · 章首");
    expect(win.textContent).toContain("某人在渡口等船");
    expect(win.textContent).toContain("按章内位置挑");
    expect(win.textContent).toContain("叙述为主");
    expect(byTestId("sr-block-card").textContent).toContain("对白常常只有半句");
    await click($(".sr-window-head", result));
    await settle();
    expect($(".sr-window-text", result).textContent).toContain("渡口的灯一盏一盏灭了。");
  });

  it("没学过：先去学习文风", async () => {
    await mountView();
    await openStage("apply");
    expect(byTestId("sr-apply-empty")).toBeTruthy();
  });
});

describe("第四步 · 对照检查", () => {
  const CHECK_READING = {
    reading_id: "sfr-1", scene_id: null, project_id: null, profile_id: "pf-a", source: "manual_check", stage: "manual",
    percentile: 41.6, within_range: true, max_percentile: 90, emphasized_dimensions: [], excluded_dimensions: ["theme.values"],
    reliable: true, unreliable_reason: null, char_count: 1800, window_count: 40,
    out_of_band: [{ feature: "fw_modal_per_1k", dimension: "language.vocabulary", dimension_label: "词汇选择", direction: "low", phrase: "语气词（吧、呢、啊、嘛……）比作者少，口气不如作者松", z: -2.6 }],
    dimension_scores: { "language.vocabulary": 6.4, "scene.dialogue": 8.1 },
    judge: { overall: 7.2, summary: "对白再松一点就更像", dimensions: { "scene.dialogue": { score: 6, note: "对白偏正式" } } },
    copy_check: { blocked: false, hits: 0, protected_hits: 0 },
  };
  const JOB = (status, extra = {}) => ({ key: "job:job-c", job_id: "job-c", kind: "check", status, book_id: "bk-a", phase: status === "running" ? "judge" : status, percent: status === "running" ? 33.3 : null, ...extra });

  async function openCheck({ applied = false } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: applied ? [{ project_id: "w1", binding_id: "bd-1", config: OWN_BINDING.config }] : [] })];
    await mountView();
    await openStage("check");
    await settle();
  }
  function routeCheck({ post, get }) {
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, opts) => (url === `${API}/checks` ? post(body) : basePost(url, body, opts)));
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === `${API}/checks/job-c` ? get() : baseGet(url)));
  }
  async function typeText(value) {
    const area = byTestId("sr-check-text");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => {
      setter.call(area, value);
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
  async function pickScene(value) {
    await click(byTestId("sr-check-mode-scene"));
    await settle();
    const select = byTestId("sr-check-scene");
    await act(async () => {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("没学过：先去学习文风", async () => {
    await mountView();
    await openStage("check");
    expect(byTestId("sr-check-empty").textContent).toContain("先在「学习文风」里学一次");
  });

  it("贴一段文字：发起作业、看进度，出结果——位次、在不在范围、越界的地方、评审、照搬、按维；不出现 z 分与旧叫法", async () => {
    await openCheck();
    expect($('.sr-step[data-stage="check"]').getAttribute("aria-label")).toBe("对照检查（随时可查）");
    expect(byTestId("sr-check-start").disabled).toBe(true);
    await typeText("潮水退下去的时候，他在闸门前站了很久。".repeat(40));
    expect(byTestId("sr-check-start").disabled).toBe(false);
    let posted = null;
    routeCheck({
      post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", state: "queued", job: JOB("running"), reading: null }); },
      get: () => Promise.resolve({ job: JOB("succeeded", { finished_at: "2026-09-23T10:00:00" }), reading: CHECK_READING }),
    });
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ text: "潮水退下去的时候，他在闸门前站了很久。".repeat(40), profile_id: "pf-a" });
    expect(byTestId("sr-check-running").textContent).toContain("模型对着原文样例评审");
    expect($('[data-activity-key="job:job-c"]')).toBeTruthy();
    expect($('.sr-step[data-stage="check"]').className).toContain("is-running");

    await settle(1600);
    const result = byTestId("sr-check-result");
    expect(result).toBeTruthy();
    expect(byTestId("sr-check-headline").textContent).toContain("第42位/ 100");
    expect(byTestId("sr-check-headline-verdict").textContent).toBe("在作者的正常范围内");
    expect(byTestId("sr-check-headline").textContent).toContain("前 90 位都算作者的正常范围");
    expect(byTestId("sr-check-gaps").textContent).toContain("词汇选择语气词（吧、呢、啊、嘛……）比作者少");
    expect(byTestId("sr-check-judge").textContent).toContain("7.2");
    expect(byTestId("sr-check-judge").textContent).toContain("对白再松一点就更像");
    expect(byTestId("sr-check-copy").textContent).toContain("没有与参考书原文连续相同的地方");
    const dialogue = $('[data-testid="sr-check-dims"] tr[data-dimension="scene.dialogue"]');
    expect(dialogue.textContent).toContain("8.1");
    expect(dialogue.textContent).toContain("对白偏正式");
    expect($('[data-testid="sr-check-dims"] tr[data-dimension="theme.values"]').textContent).toContain("不学");
    expect(result.textContent).not.toMatch(/-2\.6|z 分|注入|回测/);
  });

  it("选一场：作品用着这本书就按作品现在的设置查（不带画像）；没用就按这份画像查", async () => {
    let posted = null;
    await openCheck({ applied: true });
    routeCheck({ post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("running") }); }, get: () => Promise.resolve({ job: JOB("running") }) });
    await pickScene("sc-2");
    expect(byTestId("sr-check-form").textContent).toContain("按《北岸手记》现在用这本书的设置");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-2", project_id: "w1" });
    expect(byTestId("sr-check-running").textContent).toContain("第 1 章 · 第 2 场「夜渡」");
  });

  it("选一场而作品没用这本书：带上这份画像", async () => {
    let posted = null;
    await openCheck();
    routeCheck({ post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("running") }); }, get: () => Promise.resolve({ job: JOB("running") }) });
    await pickScene("sc-1");
    expect(byTestId("sr-check-form").textContent).toContain("没有用这本书，这次按这份文风画像查");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-1", profile_id: "pf-a", project_id: "w1" });
  });

  it("发起就被拒（没有模型）：说成中文并给「去设置模型」", async () => {
    const go = vi.fn();
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    await mountView(go);
    await openStage("check");
    await settle();
    await typeText("一段文字");
    routeCheck({
      post: () => Promise.reject(Object.assign(new Error("llm required"), { code: "STYLE_REFERENCE_LLM_REQUIRED", status: 409 })),
      get: () => Promise.resolve({}),
    });
    await click(byTestId("sr-check-start"));
    await settle();
    expect(byTestId("sr-check-error").textContent).toContain("对照检查要由模型对着原文样例评审");
    expect(byTestId("sr-check-error").textContent).not.toContain("llm required");
    await click(byTestId("sr-check-error-action"));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("评审失败：说清楚并给「重新检查」，点了再发一次同样的检查", async () => {
    await openCheck();
    await typeText("一段文字".repeat(200));
    const bodies = [];
    routeCheck({
      post: (body) => { bodies.push(body); return Promise.resolve({ job_id: "job-c", job: JOB("failed", { error: { code: "STYLE_REFERENCE_CHECK_JUDGE_FAILED", message: "judge failed", retryable: true } }) }); },
      get: () => Promise.resolve({}),
    });
    await click(byTestId("sr-check-start"));
    await settle();
    expect(byTestId("sr-check-error").textContent).toContain("模型的参考评审没有完成");
    expect(byTestId("sr-check-error-action").textContent).toBe("重新检查");
    await click(byTestId("sr-check-error-action"));
    await settle();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("没有模型：「开始对照检查」锁住并说明，不白发请求", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await openCheck();
    await typeText("一段文字");
    expect(byTestId("sr-check-no-llm").textContent).toContain("还没有接入模型");
    expect(byTestId("sr-check-start").disabled).toBe(true);
  });
});

describe("参考书活动", () => {
  it("只列作业表条目，叫法是「段落分类 / 学习文风」；失败的给「继续学习」", async () => {
    state.activity = [
      { key: "job:j1", job_id: "j1", kind: "learn", status: "failed", book_id: "bk-a", title: "甲书", resumable: true, error: { code: "STYLE_REFERENCE_LLM_REQUIRED" } },
      { key: "job:j2", job_id: "j2", kind: "classify", mode: "import", status: "running", book_id: "bk-a", title: "甲书", percent: 50, cancellable: true },
      { key: "legacy-op-key", compat_alias_of: "job:j2", kind: "import", status: "running", book_id: "bk-a", title: "甲书" },
    ];
    await mountView();
    await settle(20);
    const panel = byTestId("sr-activity");
    expect($$("[data-activity-key]", panel).map((li) => li.dataset.activityKey).sort()).toEqual(["job:j1", "job:j2"]);
    expect(panel.textContent).toContain("导入 · 段落分类");
    expect(panel.textContent).toContain("学习文风");
    expect(panel.textContent).toContain("没有完成：这一步要用模型");
    client.apiPost.mockResolvedValue({ job_id: "j3" });
    await click(byTestId("sr-activity-resume"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, { resume: true });
  });
});
