// 风格参考页单测的共用夹具（2026-09-30 从 1,680 行的 ws-styleref.test.jsx 拆出，审计 F05-26）：合成数据（中性占位，
// 不是任何真书）、接口路由、挂载与 DOM 小工具，以及每个用例前后的重置。按步拆开的 ws-styleref-*.test.jsx 共用它；
// vi.mock 只在测试文件里生效，所以 lib/client.js 与 ws-works.jsx 的桩由各测试文件自己声明，再调
// setupStyleRefSuite(workHolder)。文件名带 .test- 而不是以 .test.jsx 结尾：vitest 不把它当测试跑，工具链守卫也不把它
// 当源码模块。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, vi } from "vitest";
import * as client from "./lib/client.js";
import * as store from "./ws-styleref-store.js";
import * as fidStore from "./ws-fidelity-store.js";
// 本场预览、对照检查、「像不像」的场名读目录 store（按当前作品缓存）：每个用例重读一遍夹具里的目录
import * as catalogStore from "./ws-catalog.jsx";
import { WsStyleRef } from "./ws-styleref.jsx";

export { client, store, fidStore };

export const API = "/api/v2/style-reference";

/* ---------- 合成数据（中性占位，不是任何真书） ---------- */
export function bookRow(over = {}) {
  return {
    book_id: "bk-a", title: "甲书", author_label: "某甲", total_chars: 48000, status: "ready", cloud_policy: "allow_full_cloud",
    paragraph_count: 900, paragraph_types_revision: 2, classification_provenance: { source: "llm", llm_paragraphs: 900, agreement: 0.91 },
    classification: null, learn: null, profile: null, profile_count: 0, applied_projects: [], created_at: "2026-09-20T08:00:00Z",
    ...over,
  };
}
export const PROFILE_SUMMARY = { profile_id: "pf-a", title: "甲书 · 文风", status: "active", profile_version: "style_profile_v3", learned_at: "2026-09-21T10:00:00Z", card_lines: 3, needs_relearn: false, relearn_reason: null };

export const PROFILE_DETAIL = {
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

export const OWN_BINDING = {
  binding_id: "bd-1", profile_id: "pf-a", scope: "project", scope_ref_id: "w1", status: "active",
  config: { reference_mode: "full", sample_windows: 12, draft_mode: "style_first", dimension_states: {} },
};

export const CATALOG = {
  chapters: [
    {
      chapter_id: "c1", slug: "ch01", no: "01", title: "雾里",
      scenes: [{ scene_id: "sc-1", slug: "sc-1", title: "码头" }, { scene_id: "sc-2", slug: "sc-2", title: "夜渡" }],
    },
  ],
};

export const PREVIEW = {
  reference_mode: "full", requested_reference_mode: "full", sample_windows: 12,
  scene: { scene_id: "sc-1", found: true, position: "opening", situation_tags: ["开章引入"] },
  windows: [
    {
      window_no: 7, chapter: 3, position: "opening", start: 120, end: 150, chars: 3900, paragraphs: 31, slot: "position",
      situations: ["开章引入"], moods: ["平静"], dimensions: ["language.rhetoric", "scene.dialogue"], gist: "某人在渡口等船", paragraph_type: "narration",
    },
  ],
  blocks: { card: "[文风卡]\n· 对白常常只有半句", voice: "[声音]\n· 句尾少用语气词", samples: "……", red_line: "[红线]\n不许照搬" },
  sizes: { sample_windows: 12, sample_chars: 48000, card_chars: 20, card_lines: 3, total_chars: 51000 },
  notices: [],
};

/* ---------- 路由 ---------- */
export let state;

export function resetState() {
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

export function installRoutes() {
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

/* 卸掉已经挂上的页面（用例中途要「重新挂载」时用） */
export async function unmountAll() {
  for (const { root, host } of mounted.splice(0)) { await act(async () => root.unmount()); host.remove(); }
}
export async function mount(element) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(element); });
  mounted.push({ root, host });
  return host;
}
export async function settle(ms = 0) {
  await act(async () => { await new Promise((r) => setTimeout(r, ms)); });
}
export async function click(el) {
  expect(el, "要点的元素不存在").toBeTruthy();
  await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
}
export const $ = (sel, root = document.body) => root.querySelector(sel);
export const $$ = (sel, root = document.body) => Array.from(root.querySelectorAll(sel));
export const byTestId = (id) => $(`[data-testid="${id}"]`);
export async function openStage(id) {
  await click($(`.sr-step[data-stage="${id}"]`));
  await settle();
}
export async function mountView(go = vi.fn()) {
  const host = await mount(<WsStyleRef go={go} />);
  await settle();
  await settle();
  return host;
}

/* 每个用例前后：夹具与路由重来、store 清空、目录重读；挂上的页面卸掉。workHolder 是测试文件里 vi.hoisted 的当前作品 */
export function setupStyleRefSuite(workHolder) {
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
    catalogStore.WsCatalog.reset();
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
}
