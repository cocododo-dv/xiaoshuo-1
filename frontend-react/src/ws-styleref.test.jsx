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
    projectFidelity: {},
    catalogs: { w1: CATALOG },
    bannedTerms: [{ term_id: "t1", term: "某地", source: "protected_auto", scope: "generation" }],
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
    if (path === `${API}/profiles/pf-a/banned-terms`) return Promise.resolve({ terms: state.bannedTerms });
    if (path === `${API}/projects/w1/style-binding`) return Promise.resolve(state.projectBinding);
    m = path.match(/^\/api\/v2\/projects\/([^/]+)\/catalog$/);
    if (m) return Promise.resolve(state.catalogs[m[1]] || { chapters: [] });
    if (path === "/api/v1/projects/w1/style-fidelity") return Promise.resolve(state.projectFidelity);
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
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: [{ project_id: "w1", project_title: "北岸手记", binding_id: "bd-1", config: {} }] })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($("[data-sr-select]"));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("《北岸手记》正在用《甲书》的文风");
  });

  it("用着这本书的每一部作品都点出来，不只当前作品；页头单本删除也一样（复核 #5）", async () => {
    state.books = [bookRow({
      profile: PROFILE_SUMMARY,
      applied_projects: [
        { project_id: "w1", project_title: "北岸手记", binding_id: "bd-1", config: {} },
        { project_id: "w2", project_title: "南山", binding_id: "bd-2", config: {} },
      ],
    })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($("[data-sr-select]"));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("《北岸手记》、《南山》正在用《甲书》的文风");
    expect(confirm.mock.calls[0][0]).toContain("这些作品起草新场景时不再带它的文风");
    await click($(".sr-stage-actions .sr-menu button"));
    await click(byTestId("sr-header-delete"));
    await settle();
    expect(confirm.mock.calls[1][0]).toContain("《北岸手记》、《南山》正在用《甲书》的文风");
  });

  it("删掉的书没删成：说中文原因，不把后端的英文原话给作者看（复核 #14）", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书" })];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => (url === `${API}/books/bulk-delete`
      ? Promise.resolve({ results: [{ book_id: "bk-a", deleted: false, error: { code: "INTERNAL_ERROR", message: "IntegrityError: FOREIGN KEY constraint failed" } }], deleted_count: 0, failed_count: 1 })
      : Promise.resolve({})));
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($('[data-sr-select="bk-a"]'));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    const said = window.alert.mock.calls.map(([m]) => m).join("\n");
    expect(said).toContain("删除了 0 本，另有 1 本没删成。《甲书》：请稍后重试。");
    expect(said).not.toContain("IntegrityError");
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
    // 中间一档的名字说它真做的事：分类、学习照样发整段，只有起草不发原文（复核 #6）
    expect(policyText).toContain("起草不发原文分类和学习时正文照样发给云端");
    expect(policyText).not.toContain("只发短句");
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

  it("请求还在路上时关了对话框：失败走提示层说出来，不因再打开时清空而丢掉（复核 #16）", async () => {
    await mountView();
    await openImport();
    await click(byTestId("sr-rights-analysis"));
    await click(byTestId("sr-rights-send"));
    const input = byTestId("sr-import-file");
    Object.defineProperty(input, "files", { value: [new File(["一段正文。"], "丙书.txt", { type: "text/plain" })], configurable: true });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
    let reject;
    client.apiPost.mockImplementation((url) => (url === `${API}/books/import-upload`
      ? new Promise((_resolve, rej) => { reject = rej; })
      : Promise.resolve({})));
    await click(byTestId("sr-import-submit"));
    await click($('button[aria-label="关闭导入"]'));
    await settle();
    expect(byTestId("sr-import-submit")).toBeNull();
    await act(async () => { reject(Object.assign(new Error("empty"), { code: "STYLE_REFERENCE_BOOK_EMPTY" })); });
    await settle();
    expect(window.alert).toHaveBeenCalledWith("《丙书》没有导入：这个文件里没有可以当参考的正文。");
    // 再打开：是一次新的导入，不带上一次的错误
    await openImport();
    expect(byTestId("sr-import-error")).toBeNull();
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

  it("「用模型重新分类」失败了（书一直是 ready）：说清楚、给「继续分类」，不是只剩整本重付的重新分类（复核 #8）", async () => {
    state.books = [bookRow({
      profile: PROFILE_SUMMARY,
      classification: {
        job_id: "job-rt", mode: "retype", state: "failed", batches_done: 4, batches_total: 10, resumable: true,
        error: { code: "STYLE_REFERENCE_JOB_FAILED", message: "RuntimeError: relay closed the connection" },
      },
    })];
    await mountView();
    // 参考书一步「需处理」：落点就在这里
    expect($(".sr-step.is-active").dataset.stage).toBe("book");
    expect($('.sr-step[data-stage="book"]').getAttribute("aria-label")).toBe("参考书（需处理）");
    const card = byTestId("sr-overview-classify");
    expect(card.textContent).toContain("重新分类没完成");
    expect(card.textContent).not.toContain("模型已分好");
    expect(byTestId("sr-overview-retype-unfinished").textContent).toContain("已分好 4/10 批，继续分类只补剩下的");
    expect(byTestId("sr-overview-classify-reason").textContent).toBe("原因：后台作业出了意外停下了，可以从断点继续。");
    expect(card.textContent).not.toContain("RuntimeError");
    expect(byTestId("sr-overview-retype").textContent).toBe("从头重新分类");
    client.apiPost.mockResolvedValueOnce({ job_id: "job-rt", mode: "retype" });
    await click(byTestId("sr-overview-resume"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/reclassify`, { resume: true });
    expect(client.apiPost.mock.calls.some(([url, body]) => url.endsWith("/reclassify") && body && body.mode === "retype")).toBe(false);
  });

  it("取消了的「用模型重新分类」同样能接着分", async () => {
    state.books = [bookRow({ classification: { job_id: "job-rt", mode: "retype", state: "cancelled", batches_done: 2, batches_total: 8, resumable: true, error: { code: "STYLE_REFERENCE_JOB_CANCELLED", message: "cancelled" } } })];
    await mountView();
    await openStage("book");
    expect(byTestId("sr-overview-classify").textContent).toContain("重新分类已取消");
    expect(byTestId("sr-overview-retype-unfinished").textContent).toContain("被取消了");
    expect(byTestId("sr-overview-classify-reason")).toBeNull();
    expect(byTestId("sr-overview-resume")).toBeTruthy();
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

  it("去设置里接好模型再回来（页面重新挂载）：重读运行时，学习文风不再锁着（复核 #2）", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView();
    expect(byTestId("sr-learn-no-llm")).toBeTruthy();
    expect(byTestId("sr-learn-start").disabled).toBe(true);
    // 去设置（页面卸载），接好模型，再回来——同一次打开应用，store 没有清空
    for (const { root, host } of mounted.splice(0)) { await act(async () => root.unmount()); host.remove(); }
    state.runtime = { llm_enabled: true, llm_is_local: false, default_cloud_policy: "allow_full_cloud" };
    await mountView();
    await settle();
    expect(byTestId("sr-learn-no-llm")).toBeNull();
    expect(byTestId("sr-learn-start").disabled).toBe(false);
  });

  it("活动清单读不到、书库摘要说在学：照样显示「学习中」（复核 #10）", async () => {
    state.books = [bookRow({ learn: { job_id: "job-l7", state: "running", done: 3, total: 7, phase_label: "学习文风 · 给片段打标签" } })];
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === `${API}/activity`
      ? Promise.reject(Object.assign(new Error("down"), { code: "NETWORK_ERROR" }))
      : baseGet(url)));
    await mountView();
    expect(byTestId("sr-learn-status").textContent).toBe("学习中 43%");
    expect(byTestId("sr-learn-running").textContent).toContain("给片段打标签 3/7");
    expect($('[data-activity-key="job:job-l7"]')).toBeTruthy();
  });

  it("上次学习失败的原因只说中文：作业边界记下的英文原话不给作者看（复核 #14）", async () => {
    state.books = [bookRow({ learn: { job_id: "job-l8", state: "failed", resumable: true, error: { code: "STYLE_REFERENCE_JOB_FAILED", message: "KeyError: 'windows'" } } })];
    state.learn = { ...state.learn, learn: { job_id: "job-l8", state: "failed", resumable: true, error: { code: "", message: "KeyError: 'windows'" } } };
    await mountView();
    const line = byTestId("sr-learn-last-error").textContent;
    expect(line).toBe("上次学习没有完成：出了意外停下了，可以从断点继续。");
    expect(document.body.textContent).not.toContain("KeyError");
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

  it("作品沿用的是旧版全局应用：逐维状态只读，不往那条全局应用上写（复核 #3）", async () => {
    const GLOBAL = { ...OWN_BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null, config: { ...OWN_BINDING.config, dimension_states: { "scene.dialogue": "emphasize" } } };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = { project_id: "w1", binding: GLOBAL, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    await mountView();
    await openStage("learn");
    await settle();
    expect(byTestId("sr-states-legacy").textContent).toContain("沿用的是一条旧版的「全部作品」应用");
    expect(byTestId("sr-states-hint")).toBeNull();
    expect(byTestId("sr-dim-state-scene.dialogue-emphasize")).toBeNull();
    // 全局应用上标的「重点」照样显示（它在起草时照样起作用）
    expect($('.sr-dim[data-dimension="scene.dialogue"] .sr-dim-head').textContent).toContain("重点");
    expect(client.apiPatch).not.toHaveBeenCalled();
  });

  it("本书专名可以逐个去掉：先说清楚去掉之后不再受保护（复核 #12）", async () => {
    await openPortrait();
    const banned = byTestId("sr-banned");
    expect(banned.textContent).toContain("本书专名 1 个");
    await click([...banned.querySelectorAll("button")].find((b) => b.textContent === "看看"));
    const chip = $('[data-term-id="t1"]');
    expect(chip.textContent).toContain("某地");
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await click($('[data-testid="sr-protected-remove"]', chip));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("不再保护「某地」？");
    expect(confirm.mock.calls[0][0]).toContain("以后重新学习文风也不会再把它加回来");
    expect(client.apiDelete).not.toHaveBeenCalled();
    confirm.mockReturnValueOnce(true);
    state.bannedTerms = [];
    await click($('[data-testid="sr-protected-remove"]', $('[data-term-id="t1"]')));
    await settle();
    expect(client.apiDelete).toHaveBeenCalledWith(`${API}/banned-terms/t1`);
    expect(byTestId("sr-banned").textContent).toContain("本书专名 0 个");
  });

  it("重新学习（同一份画像）学完：本书专名那一组重读（复核 #13）", async () => {
    await openPortrait();
    const termCalls = () => client.apiGet.mock.calls.filter(([url]) => url === `${API}/profiles/pf-a/banned-terms`).length;
    const before = termCalls();
    expect(byTestId("sr-banned").textContent).toContain("本书专名 1 个");
    state.bannedTerms = [
      { term_id: "t2", term: "某城", source: "protected_auto", scope: "generation" },
      { term_id: "t3", term: "某人", source: "protected_auto", scope: "generation" },
    ];
    // 这本书的学习作业跑完（活动表从在跑走到成功）
    await act(async () => {
      store.srActivityApply([{ key: "job:jl", job_id: "jl", kind: "learn", status: "running", book_id: "bk-a", profile_id: "pf-a" }]);
      store.srActivityApply([{ key: "job:jl", job_id: "jl", kind: "learn", status: "succeeded", book_id: "bk-a", profile_id: "pf-a", result: { profile_id: "pf-a" } }]);
    });
    await settle(20);
    expect(termCalls()).toBeGreaterThan(before);
    expect(byTestId("sr-banned").textContent).toContain("本书专名 2 个");
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

describe("文风画像 · 当前作品像不像", () => {
  const WORK_FIDELITY = {
    project_id: "w1", bound: true, profile_id: "pf-a", reading_count: 5, final_scene_count: 2,
    trend: [
      { reading_id: "t1", scene_id: "sc-1", stage: "first_draft", percentile: 93, within_range: false, reliable: true, max_percentile: 90, created_at: "2026-09-22T08:00:00" },
      { reading_id: "t2", scene_id: "sc-1", stage: "final", percentile: 55, within_range: true, reliable: true, max_percentile: 90, created_at: "2026-09-22T09:00:00" },
      { reading_id: "t3", scene_id: "sc-2", stage: "final", percentile: 96, within_range: false, reliable: true, max_percentile: 90, created_at: "2026-09-23T09:00:00" },
    ],
    recent_gaps: ["对白比作者少"],
    recent_gap_details: [{ feature: "dialogue_char_share", direction: "low", dimension: "scene.dialogue", dimension_label: "对话写法", phrase: "对白比作者少", hits: 3, window: 4 }],
    dimension_averages: {
      "scene.dialogue": { label: "对话写法", deterministic: 6.2, judge: 6.5, scenes: 2, judged: 2 },
      "language.sentence_structure": { label: "句式结构", deterministic: 8.1, judge: null, scenes: 2, judged: 0 },
    },
    scene_finals: {
      "sc-1": { reading_id: "t2", percentile: 55, within_range: true, reliable: true },
      "sc-2": { reading_id: "t3", percentile: 96, within_range: false, reliable: true },
    },
  };

  async function openPortrait({ applied = true } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: applied ? [{ project_id: "w1", binding_id: "bd-1", config: OWN_BINDING.config }] : [] })];
    if (applied) state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    state.projectFidelity = WORK_FIDELITY;
    await mountView();
    await openStage("learn");
    await settle();
    await settle();
  }
  const dimRow = (dim) => $(`.sr-dim[data-dimension="${dim}"]`);

  it("用于当前作品时：顶上一张卡——终稿几场在作者范围内、近期常见偏差、走势（可切表格，场名来自目录）", async () => {
    await openPortrait();
    const card = byTestId("sr-work-fidelity");
    expect(card.textContent).toContain("《北岸手记》像不像");
    expect(byTestId("sr-work-fid-finals").textContent).toContain("1 / 2");
    expect(byTestId("sr-work-fid-gaps").textContent).toContain("1");
    expect(card.textContent).toContain("对话写法对白比作者少（3 次）");
    const trend = byTestId("sr-work-trend");
    expect($(".fid-trend-plot > svg", trend).getAttribute("aria-label")).toBe("最近 3 次读数：终稿 2 次，其中 1 次在作者的正常范围内；最近一次终稿第 96 位");
    expect(trend.textContent).toContain("作者的正常范围（前 90 位）");
    await click(byTestId("sr-work-trend-table-toggle"));
    const rows = $$("tbody tr", byTestId("sr-work-trend-table"));
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("第 1 章 · 第 2 场「夜渡」");
    expect(rows[0].textContent).toContain("第 96 位");
    expect(rows[0].textContent).toContain("超出范围");
    expect(rows[2].textContent).toContain("首稿");
  });

  it("每一维右边是作品在这一维的平均分；近期常见偏差标出来，展开说是哪一句、几次", async () => {
    await openPortrait();
    const dialogue = dimRow("scene.dialogue");
    const chip = $('[data-testid="sr-dim-fid"]', dialogue);
    expect(chip.textContent).toContain("测得6.2");
    expect(chip.textContent).toContain("评审6.5");
    expect(chip.textContent).toContain("近期常见偏差");
    const sentence = $('[data-testid="sr-dim-fid"]', dimRow("language.sentence_structure"));
    expect(sentence.textContent).toContain("测得8.1");
    expect(sentence.textContent).not.toContain("评审");
    await click($(".sr-dim-toggle", dialogue));
    const body = $('[data-testid="sr-dim-fid-body"]', dialogue);
    expect(body.textContent).toContain("《北岸手记》在这一维");
    expect(body.textContent).toContain("测得平均 6.2 / 10（2 场终稿）");
    expect(body.textContent).toContain("近期常见偏差：对白比作者少（最近 4 次首稿里 3 次）");
  });

  it("这本书没用于当前作品：不挂卡、不挂分数（读数是对着作品现在用的那份画像量的）", async () => {
    await openPortrait({ applied: false });
    expect(byTestId("sr-work-fidelity")).toBeNull();
    expect($('[data-testid="sr-dim-fid"]')).toBeNull();
    expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/projects/w1/style-fidelity")).toBe(false);
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

  it("「起草不发原文」的书：带原文的两种方式用不了", async () => {
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

  it("作品沿用旧版全局应用：只读，不 PATCH / 不 DELETE 那条全局应用；「用于」给这部作品单独建一条（复核 #3）", async () => {
    const GLOBAL = {
      ...OWN_BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null,
      config: { reference_mode: "samples_only", sample_windows: 6, draft_mode: "style_first", dimension_states: { "scene.dialogue": "emphasize" } },
    };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = { project_id: "w1", binding: GLOBAL, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    state.profileBindings = [GLOBAL];
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-legacy").textContent).toContain("沿用一条旧版的「全部作品」应用，用的就是这本书的文风");
    expect(byTestId("sr-apply-unbind")).toBeNull();
    const submit = byTestId("sr-apply-submit");
    expect(submit.textContent).toContain("用于《北岸手记》");
    expect(submit.disabled).toBe(false);
    // 表单沿用全局应用的设置
    expect(byTestId("sr-sample-windows").value).toBe("6");
    // 「这份文风还用在」里如实列出它（在那里解除才是对全部作品）
    expect(byTestId("sr-apply-others").textContent).toContain("没有自己应用的所有作品");
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) return Promise.resolve({ binding: { ...OWN_BINDING, binding_id: "bd-own" }, created: true, changed: true, replaced: [] });
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    await click(submit);
    await settle();
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(client.apiDelete).not.toHaveBeenCalled();
    const [, body] = client.apiPost.mock.calls.find(([url]) => url.endsWith("/apply"));
    expect(body.scope).toBe("project");
    expect(body.scope_ref_id).toBe("w1");
    expect(body.config).toMatchObject({ reference_mode: "samples_only", sample_windows: 6, draft_mode: "style_first" });
    expect(body.config.dimension_states["scene.dialogue"]).toBe("emphasize");
  });

  it("换回以前用过的这本：表单按它在这部作品上停用的那条应用的设置填，用上就按它来（复核 #4）", async () => {
    const STORED = {
      ...OWN_BINDING, binding_id: "bd-old", status: "disabled",
      config: { reference_mode: "samples_only", sample_windows: 5, draft_mode: "neutral_first", dimension_states: { "theme.values": "exclude" } },
    };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = {
      project_id: "w1", binding: { ...OWN_BINDING, binding_id: "bd-x", profile_id: "pf-x" }, profile: { profile_id: "pf-x" }, book: { book_id: "bk-x", title: "丁书" },
    };
    state.profileBindings = [STORED];
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-other").textContent).toContain("用这本会替换它（《丁书》在《北岸手记》上的设置保留，以后换回来还在）");
    expect(byTestId("sr-apply-stored").textContent).toContain("这本书上次用于《北岸手记》时的设置已经填在下面");
    expect($('[data-testid="sr-reference-mode"] [data-value="samples_only"]').getAttribute("aria-checked")).toBe("true");
    expect(byTestId("sr-sample-windows").value).toBe("5");
    expect($('[data-testid="sr-draft-mode"] [data-value="neutral_first"]').getAttribute("aria-checked")).toBe("true");
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) return Promise.resolve({ binding: { ...STORED, status: "active" }, created: false, changed: true, replaced: [{ binding_id: "bd-x", profile_id: "pf-x", book_title: "丁书" }] });
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    await click(byTestId("sr-apply-submit"));
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/apply`, {
      scope: "project", scope_ref_id: "w1", config: { reference_mode: "samples_only", sample_windows: 5, draft_mode: "neutral_first" },
    });
    expect(byTestId("sr-apply-result").textContent).toContain("按这本书上次用于它时的设置");
  });

  it("用于之后「这份文风还用在」还在（清单重读，不是删掉了事）（复核 #9）", async () => {
    const SCENE_BINDING = { ...OWN_BINDING, binding_id: "bd-s", scope: "scene", scope_ref_id: "sc-9" };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.profileBindings = [SCENE_BINDING];
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-others").textContent).toContain("某一场");
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) {
        state.profileBindings = [SCENE_BINDING, OWN_BINDING];
        state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
        return Promise.resolve({ binding: OWN_BINDING, created: true, changed: true, replaced: [] });
      }
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    await click(byTestId("sr-apply-submit"));
    await settle();
    await settle();
    expect(byTestId("sr-apply-result")).toBeTruthy();
    expect(byTestId("sr-apply-others")).toBeTruthy();
    expect(byTestId("sr-apply-others").textContent).toContain("某一场");
  });

  it("「起草不发原文」的书：状态行按真正生效的参考方式说（只用文风卡），不说「全面模仿 · 12 窗」（复核 #11）", async () => {
    await openApply({ own: true, bookOver: { cloud_policy: "segments_only" } });
    // 后端给的 effective_reference_mode 在这里缺席也照样按书的原文范围推
    expect(byTestId("sr-apply-status").textContent).toContain("正在用这本书的文风：只用文风卡（这本书起草不发原文） · 作者手笔直起");
    expect(byTestId("sr-apply-status").textContent).not.toContain("12 窗");
    expect(byTestId("sr-apply-segments-only").textContent).toContain("这本书导入时选了「起草不发原文」");
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

  it("选一场而作品用的是这本书的旧版全局应用：同样按作品现在的设置查（不带画像）", async () => {
    let posted = null;
    state.projectBinding = {
      project_id: "w1",
      binding: { ...OWN_BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null },
      profile: PROFILE_SUMMARY,
      book: { book_id: "bk-a", title: "甲书" },
    };
    await openCheck();
    routeCheck({ post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("running") }); }, get: () => Promise.resolve({ job: JOB("running") }) });
    await pickScene("sc-2");
    expect(byTestId("sr-check-form").textContent).toContain("按《北岸手记》现在用这本书的设置");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-2", project_id: "w1" });
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

  it("换了作品：上一部作品里选的那一场不带过来，按钮也不能点（复核 #7）", async () => {
    let posted = null;
    state.catalogs.w2 = { chapters: [{ chapter_id: "c9", no: 1, title: "南风", scenes: [{ scene_id: "sc-9", title: "河口" }] }] };
    await openCheck();
    routeCheck({
      post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("succeeded", { finished_at: "2026-09-23T10:00:00" }), reading: { ...CHECK_READING, scene_id: "sc-2" } }); },
      get: () => Promise.resolve({}),
    });
    await pickScene("sc-2");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-2", profile_id: "pf-a", project_id: "w1" });
    expect(byTestId("sr-check-result").textContent).toContain("第 1 章 · 第 2 场「夜渡」");
    // 换到另一部作品
    workHolder.current = { id: "w2", title: "南山" };
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:work-changed")); });
    await settle();
    await settle();
    const select = byTestId("sr-check-scene");
    expect($$("option", select).map((o) => o.value)).toEqual(["", "sc-9"]);
    expect(select.value).toBe("");
    expect(byTestId("sr-check-start").disabled).toBe(true);
    // 上一次的结果还挂着，但说清楚那是另一部作品的一场，不冒充当前作品
    expect(byTestId("sr-check-result").textContent).toContain("另一部作品的一场");
    expect(byTestId("sr-check-result").textContent).not.toContain("当前作品的一场");
  });
});

describe("参考书活动", () => {
  it("对照检查的条目：叫「对照检查」，做完「打开」落在这本书的「对照检查」", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.activity = [{ key: "job:jc", job_id: "jc", kind: "check", status: "succeeded", book_id: "bk-a", title: "甲书", percent: 100 }];
    await mountView();
    await settle(20);
    // 打开页面之前就已结束的条目收在「更早结束的」里
    await click($(".sr-activity-older-toggle"));
    const item = $('[data-activity-key="job:jc"]');
    expect(item.textContent).toContain("对照检查");
    expect($("[data-testid=\"sr-activity-cancel\"]", item)).toBeNull();
    expect($("[data-testid=\"sr-activity-resume\"]", item)).toBeNull();
    const open = [...item.querySelectorAll("button")].find((b) => b.textContent === "打开");
    await click(open);
    await settle();
    expect($(".sr-step.is-active").dataset.stage).toBe("check");
    expect(byTestId("sr-check-form")).toBeTruthy();
  });

  it("只列作业表条目，叫法是「段落分类 / 学习文风」；失败的给「继续学习」", async () => {
    state.activity = [
      { key: "job:j1", job_id: "j1", kind: "learn", status: "failed", book_id: "bk-a", title: "甲书", resumable: true, error: { code: "STYLE_REFERENCE_LLM_REQUIRED" } },
      { key: "job:j2", job_id: "j2", kind: "classify", mode: "import", status: "running", book_id: "bk-a", title: "甲书", percent: 50, cancellable: true },
      { key: "legacy-op-key", kind: "import", status: "running", book_id: "bk-a", title: "甲书" },
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

  it("失败原因是英文原话（作业边界记下的异常串）时说中文（复核 #14）", async () => {
    state.activity = [
      { key: "job:j1", job_id: "j1", kind: "classify", mode: "retype", status: "failed", book_id: "bk-a", title: "甲书", resumable: true, error: { code: "", message: "TypeError: 'NoneType' object is not iterable" } },
    ];
    await mountView();
    await settle(20);
    const item = $('[data-activity-key="job:j1"]');
    expect(item.textContent).toContain("没有完成：出了意外停下了，可以从断点继续。");
    expect(byTestId("sr-activity").textContent).not.toMatch(/TypeError|NoneType/);
  });

  it("「继续学习」沿用同一个作业：条目换成排队、不被关掉；很快跑完的结果照常显示（复核 #15）", async () => {
    state.activity = [
      { key: "job:j1", job_id: "j1", kind: "learn", status: "failed", book_id: "bk-a", title: "甲书", resumable: true, error: { code: "STYLE_REFERENCE_LEARN_LLM_CALL_FAILED", message: "x" } },
    ];
    await mountView();
    await settle(20);
    // 续跑的同一个作业在第一次轮询之前就跑完了
    state.activity = [{ key: "job:j1", job_id: "j1", kind: "learn", status: "succeeded", book_id: "bk-a", title: "甲书", percent: 100, result: { profile_id: "pf-a" } }];
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "j1", state: "queued" } : {}));
    await click(byTestId("sr-activity-resume"));
    await settle(30);
    const item = $('[data-activity-key="job:j1"]');
    expect(item, "续跑的条目被当成「关掉过」丢掉了").toBeTruthy();
    expect(item.dataset.activityStatus).toBe("succeeded");
    expect(item.textContent).toContain("完成");
  });
});
