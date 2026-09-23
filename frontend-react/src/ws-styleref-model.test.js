// 风格参考 · 说法与纯派生（ws-styleref-model.js）：绑定配置四个旋钮与后端 normalize_binding_config 同一口径、
// 出错说法按错误码给中文（认不出的英文原话不给作者看）、一本书走到哪一步、活动条目的文案。
import { describe, expect, it } from "vitest";
import { STYLE_DIMENSIONS } from "./ws-labels.js";
import {
  SR_CLOUD_POLICIES,
  SR_REFERENCE_MODES,
  srActivityKindLabel,
  srActivityView,
  srAppliedToWork,
  srBookPipeline,
  srClassifyEstimateText,
  srConfigSummary,
  srDefaultConfig,
  srDimensionGroups,
  srDimensionStatesSummary,
  srErrorInfo,
  srFilterBooks,
  srFormatChars,
  srFormatCount,
  srFormatMinutes,
  srFormatPct,
  srLandingStage,
  srLearnEstimateText,
  srNormalizeConfig,
  srPickLandingBook,
  srProvenanceView,
  srReadUiPrefs,
  srRelearnText,
  srRememberUi,
  srRightsReady,
  srSettingsEqual,
  srSortBooks,
  srStageStates,
} from "./ws-styleref-model.js";

const book = (over = {}) => ({
  id: "b1", title: "样书", rawStatus: "ready", provenance: { source: "llm" }, profile: null, learn: null,
  appliedProjects: [], ...over,
});
const profile = (over = {}) => ({ profile_id: "p1", needs_relearn: false, relearn_reason: null, ...over });

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
  };
}

describe("绑定配置：四个旋钮", () => {
  it("缺键补默认值：全面模仿 · 12 窗 · 16 维都正常 · 作者手笔直起", () => {
    const c = srNormalizeConfig(null);
    expect(c).toEqual(srDefaultConfig());
    expect(c.reference_mode).toBe("full");
    expect(c.sample_windows).toBe(12);
    expect(c.draft_mode).toBe("style_first");
    expect(Object.keys(c.dimension_states)).toEqual(STYLE_DIMENSIONS);
    expect(new Set(Object.values(c.dimension_states))).toEqual(new Set(["normal"]));
  });

  it("窗数夹在 0–16 并取整；认不出的参考方式 / 起草方式 / 维度 / 状态一律回默认，不透传", () => {
    expect(srNormalizeConfig({ sample_windows: 40 }).sample_windows).toBe(16);
    expect(srNormalizeConfig({ sample_windows: -3 }).sample_windows).toBe(0);
    expect(srNormalizeConfig({ sample_windows: 7.6 }).sample_windows).toBe(8);
    expect(srNormalizeConfig({ sample_windows: 0 }).sample_windows).toBe(0);
    expect(srNormalizeConfig({ sample_windows: "abc" }).sample_windows).toBe(12);
    expect(srNormalizeConfig({ reference_mode: "MIXED" }).reference_mode).toBe("full");
    expect(srNormalizeConfig({ draft_mode: "whatever" }).draft_mode).toBe("style_first");
    const states = srNormalizeConfig({
      dimension_states: { "scene.dialogue": "emphasize", "theme.values": "exclude", "bogus.dim": "exclude", "language.vocabulary": "loud" },
    }).dimension_states;
    expect(states["scene.dialogue"]).toBe("emphasize");
    expect(states["theme.values"]).toBe("exclude");
    expect(states["language.vocabulary"]).toBe("normal");
    expect(states).not.toHaveProperty("bogus.dim");
  });

  it("三种参考方式各一句真话；只比三个顶层旋钮", () => {
    expect(SR_REFERENCE_MODES.map((m) => m.id)).toEqual(["full", "samples_only", "card_only"]);
    expect(SR_REFERENCE_MODES.find((m) => m.id === "card_only").detail).toContain("不发原文");
    expect(srSettingsEqual({ sample_windows: 12 }, {})).toBe(true);
    expect(srSettingsEqual({ sample_windows: 11 }, {})).toBe(false);
    expect(srSettingsEqual({ dimension_states: { "scene.dialogue": "exclude" } }, {})).toBe(true);
  });

  it("一句话汇总：只用文风卡时不说窗数；维度状态按「几维重点 · 几维不学」", () => {
    expect(srConfigSummary({})).toBe("全面模仿 · 12 窗 · 作者手笔直起");
    expect(srConfigSummary({ reference_mode: "card_only", draft_mode: "neutral_first" })).toBe("只用文风卡 · 先中性后润色");
    expect(srDimensionStatesSummary({})).toBe("16 维都按正常学");
    expect(srDimensionStatesSummary({ "scene.dialogue": "emphasize", "theme.values": "exclude", "theme.motifs": "exclude" }))
      .toBe("1 维重点 · 2 维不学");
  });
});

describe("导入的三档原文范围与权属", () => {
  it("三档说法：仅本机模型 / 只发短句（起草时只用文风卡，不发原文）/ 可发送全文", () => {
    expect(SR_CLOUD_POLICIES.map((p) => [p.id, p.label])).toEqual([
      ["local_only", "仅本机模型"], ["segments_only", "只发短句"], ["allow_full_cloud", "可发送全文"],
    ]);
    expect(SR_CLOUD_POLICIES[1].hint).toBe("起草时只用文风卡，不发原文");
  });

  it("分析权必勾；只有「仅本机模型」不要发送权", () => {
    expect(srRightsReady("local_only", { analysis_rights: true })).toBe(true);
    expect(srRightsReady("segments_only", { analysis_rights: true })).toBe(false);
    expect(srRightsReady("allow_full_cloud", { analysis_rights: true, send_rights: true })).toBe(true);
    expect(srRightsReady("local_only", {})).toBe(false);
  });
});

describe("出错说法", () => {
  it("认得的错误码给固定中文；认不出的英文原话不给作者看，中文原话照用", () => {
    expect(srErrorInfo({ code: "STYLE_REFERENCE_BOOK_NOT_READY", message: "book not ready" }).message)
      .toBe("这本书的段落分类还没完成：等它完成（或「继续分类」）之后再学。");
    expect(srErrorInfo({ code: "SOMETHING_NEW", message: "Internal error: boom" }).message).toBe("操作没有完成，请稍后重试。");
    expect(srErrorInfo({ code: "SOMETHING_NEW", message: "服务器说：换一本书" }).message).toBe("服务器说：换一本书");
    expect(srErrorInfo(null, "兜底").message).toBe("兜底");
  });

  it("重复导入：说出书名并给「打开这本」", () => {
    const info = srErrorInfo({
      code: "STYLE_REFERENCE_BOOK_DUPLICATE", message: "dup", details: { book_id: "b9", title: "旧书" },
    });
    expect(info.message).toBe("书库里已经有同一份文本：《旧书》。");
    expect(info.action).toEqual({ type: "open_book", label: "打开这本", bookId: "b9" });
  });

  it("后端的 author_action 指向系统配置 → 「去设置模型」；指向学习 → 「去学习文风」", () => {
    expect(srErrorInfo({ code: "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED", details: { author_action: { view: "systemConfig" } } }).action)
      .toEqual({ type: "settings", label: "去设置模型" });
    expect(srErrorInfo({ code: "STYLE_REFERENCE_LLM_REQUIRED" }).action).toEqual({ type: "settings", label: "去设置模型" });
    expect(srErrorInfo({ code: "STYLE_REFERENCE_PROFILE_STALE", details: { author_action: { action: "learn_style", book_id: "b1" } } }).action)
      .toEqual({ type: "learn", label: "去学习文风", bookId: "b1" });
    expect(srErrorInfo({ code: "STYLE_REFERENCE_BOOK_NOT_FOUND" }).action).toBeNull();
  });
});

describe("估算与格式", () => {
  it("重新分类的估算说调用数、token、分钟与并行路数", () => {
    const text = srClassifyEstimateText({ est_calls: 120, est_input_tokens: 350000, est_output_tokens: 9000, est_minutes: 75, parallel: 3 });
    expect(text).toBe("约 120 次模型调用，输入约 35 万 token、输出约 9,000 token，约 1 小时 15 分钟（3 路并行，重试不计在内）");
    expect(srClassifyEstimateText(null)).toBeNull();
  });

  it("学一次：标签批数已知时给总数，未知时说「至少」", () => {
    expect(srLearnEstimateText({ est_calls: 12, calls: { extract: 4, tags: 6 }, est_input_chars: { extract_per_call: 44000 } }))
      .toBe("约 12 次模型调用（分层读原文 4 次、写文风卡 1 次、识别本书专名 1 次、给全书片段打标签 6 批），每层读约 4.4 万字原文，重试不计在内");
    expect(srLearnEstimateText({ est_calls: null, calls: { extract: 4, tags: null } }))
      .toBe("至少 6 次模型调用，另加给全书片段打标签（整理完窗口才知道要几批）");
    // 书比每层的读取上限短：按全书字数说，不夸大
    expect(srLearnEstimateText({ est_calls: 7, calls: { extract: 4, tags: 1 }, est_input_chars: { extract_per_call: 44000 } }, { bookChars: 5257 }))
      .toContain("每层读约 5,257 字原文");
  });

  it("百分比、分钟、字数", () => {
    expect(srFormatPct(0.934)).toBe("93%");
    expect(srFormatPct(0.056)).toBe("5.6%");
    expect(srFormatPct("x")).toBe("—");
    expect(srFormatMinutes(0.4)).toBe("不到 1 分钟");
    expect(srFormatMinutes(120)).toBe("约 2 小时");
    expect(srFormatCount(12345)).toBe("1.2 万");
    expect(srFormatCount(250000)).toBe("25 万");
    expect(srFormatCount(980)).toBe("980");
    expect(srFormatChars(48000)).toBe("4.8 万字");
    expect(srFormatChars(980)).toBe("980 字");
    expect(srFormatChars(-1)).toBe("—");
  });

  it("为什么建议重新学习", () => {
    expect(srRelearnText("types_changed")).toBe("段落类型已更新，建议重新学习：挑样本、打标签都要看段落类型。");
    expect(srRelearnText("legacy_profile")).toContain("旧版画像");
    expect(srRelearnText("nope")).toBeNull();
  });
});

describe("段落类型来源", () => {
  it("旧版启发式导入要提醒，并带出一致率", () => {
    const view = srProvenanceView({ source: "legacy_heuristic", agreement: 0.62, heuristic_paragraphs: 800, llm_paragraphs: 200 });
    expect(view).toMatchObject({ kind: "legacy_heuristic", legacy: true, agreement: 0.62, heuristicParagraphs: 800, llmParagraphs: 200 });
    expect(srProvenanceView({ source: "llm", llm_paragraphs: 1000 })).toMatchObject({ kind: "llm", legacy: false });
    expect(srProvenanceView(null)).toMatchObject({ kind: "unknown", legacy: false });
  });
});

describe("一本书走到哪一步", () => {
  it("书库徽标：分类中 / 学习中 / 当前作品在用 / 已学好 / 建议重学 / 待学习", () => {
    expect(srBookPipeline(book(), {}).label).toBe("待学习");
    expect(srBookPipeline(book(), { running: { classify: { percentText: "40%" } } }).label).toBe("分类中 40%");
    expect(srBookPipeline(book({ rawStatus: "failed" })).label).toBe("分类未完成");
    expect(srBookPipeline(book(), { running: { learn: { percentText: "10%" } } }).label).toBe("学习中 10%");
    expect(srBookPipeline(book({ profile: profile() })).label).toBe("已学好");
    expect(srBookPipeline(book({ profile: profile({ needs_relearn: true, relearn_reason: "types_changed" }) })).label).toBe("建议重新学习");
    expect(srBookPipeline(book({ profile: profile({ needs_relearn: true, relearn_reason: "legacy_profile" }) })).label).toBe("旧版画像");
    const applied = book({ profile: profile(), appliedProjects: [{ project_id: "w1", binding_id: "bd1" }] });
    expect(srBookPipeline(applied, { workId: "w1" }).label).toBe("当前作品在用");
    expect(srBookPipeline(applied, { workId: "w2" }).label).toBe("已学好");
    expect(srBookPipeline(book({ learn: { state: "failed" } })).label).toBe("学习未完成");
  });

  it("步骤条：分类没完成时学习等前一步；没画像时用于作品、对照检查等前一步；落点是第一个没做完的步", () => {
    expect(srStageStates(book({ rawStatus: "ingesting" }))).toEqual({ book: "running", learn: "blocked", apply: "blocked", check: "blocked" });
    expect(srStageStates(book({ provenance: { source: "legacy_heuristic" } })).book).toBe("attention");
    const learned = srStageStates(book({ profile: profile() }), { workId: "w1" });
    expect(learned).toEqual({ book: "done", learn: "done", apply: "todo", check: "open" });
    expect(srStageStates(book({ profile: profile() }), { running: { check: { percentText: "33%" } } }).check).toBe("running");
    expect(srLandingStage(learned)).toBe("apply");
    // 对照检查不是要做完的一步：前三步都做完了也不落在它上面
    expect(srLandingStage({ book: "done", learn: "done", apply: "done", check: "open" })).toBe("apply");
    expect(srLandingStage(srStageStates(book()))).toBe("learn");
    expect(srLandingStage(learned, { applied: true })).toBe("apply");
    const stale = srStageStates(book({ profile: profile({ needs_relearn: true }) }));
    expect(stale.learn).toBe("attention");
    expect(srAppliedToWork(book({ appliedProjects: [{ project_id: "w1" }] }), "w1")).toEqual({ project_id: "w1" });
  });

  it("文风画像按层分组，层按固定顺序、层内保持后端给的辨识度顺序", () => {
    const groups = srDimensionGroups([
      { dimension: "theme.values" }, { dimension: "language.vocabulary" }, { dimension: "language.rhetoric" },
    ]);
    expect(groups.map((g) => g.label)).toEqual(["语言", "主题"]);
    expect(groups[0].dims.map((d) => d.dimension)).toEqual(["language.vocabulary", "language.rhetoric"]);
  });
});

describe("参考书活动的文案", () => {
  it("作业叫法：导入 · 段落分类 / 用模型重新分类 / 学习文风 / 对照检查", () => {
    expect(srActivityKindLabel({ kind: "classify", mode: "import" })).toBe("导入 · 段落分类");
    expect(srActivityKindLabel({ kind: "classify", mode: "retype" })).toBe("用模型重新分类");
    expect(srActivityKindLabel({ kind: "classify", mode: "reclassify" })).toBe("段落分类");
    expect(srActivityKindLabel({ kind: "learn" })).toBe("学习文风");
    expect(srActivityKindLabel({ kind: "check" })).toBe("对照检查");
  });

  it("进行中：阶段（去掉重复的作业名）+ 步数 + 用时 + 预计 + 调用次数；失败给中文原因", () => {
    const running = srActivityView({
      kind: "learn", status: "running", phase_label: "学习文风 · 分层读原文", percent: 45.5,
      steps: { done: 2, total: 4 }, elapsed_seconds: 125, eta_seconds: 60, llm_calls: 3,
    });
    expect(running.percent).toBe(46);
    expect(running.active).toBe(true);
    expect(running.detail).toBe("分层读原文 2/4 · 已用 2:05 · 预计还需 1:00 · 模型调用 3 次");
    expect(srActivityView({ status: "succeeded", percent: 40 }).percent).toBe(100);
    expect(srActivityView({ status: "running", percent: 100 }).percent).toBe(99);
    const failed = srActivityView({ kind: "classify", status: "failed", error: { code: "STYLE_REFERENCE_LLM_REQUIRED", message: "no llm" }, elapsed_seconds: 3 });
    expect(failed.detail).toBe("没有完成：这一步要用模型，但还没有接入可用的模型。 · 用时 0:03");
    expect(srActivityView({ status: "running", stalled: true, cancel_requested: true, phase_label: "排队" }).detail)
      .toContain("正在取消 · 后台进程重启过，稍后自动接着跑");
  });
});

describe("书库排序、筛选与落点", () => {
  const books = [
    book({ id: "a", title: "甲书", author: "某甲" }),
    book({ id: "b", title: "乙书", author: "某乙", appliedProjects: [{ project_id: "w1" }] }),
  ];

  it("当前作品在用的书排最前；按书名 / 作者筛", () => {
    expect(srSortBooks(books, "w1").map((b) => b.id)).toEqual(["b", "a"]);
    expect(srSortBooks(books, null).map((b) => b.id)).toEqual(["a", "b"]);
    expect(srFilterBooks(books, "某乙").map((b) => b.id)).toEqual(["b"]);
    expect(srFilterBooks(books, "  ")).toBe(books);
  });

  it("落点：本次会话 → 当前作品在用 → 这部作品上次 → 上次 → 第一本", () => {
    expect(srPickLandingBook(books, { workId: "w1", session: { bookId: "a", stage: "learn" } })).toMatchObject({ bookId: "a", source: "session" });
    expect(srPickLandingBook(books, { workId: "w1" })).toMatchObject({ bookId: "b", source: "applied" });
    expect(srPickLandingBook(books, { workId: "w2", prefs: { last: { bookId: "b" }, works: { w2: { bookId: "a", stage: "apply" } } } }))
      .toMatchObject({ bookId: "a", stage: "apply", source: "work" });
    expect(srPickLandingBook(books, { workId: "w3", prefs: { last: { bookId: "b", stage: null }, works: {} } })).toMatchObject({ bookId: "b", source: "last" });
    expect(srPickLandingBook(books, { workId: "w3" })).toMatchObject({ bookId: "a", source: "first" });
    expect(srPickLandingBook([], {})).toBeNull();
  });

  it("界面偏好 ws_sr_ui_v1：记住每部作品上次看的书与步骤，坏数据当没有", () => {
    const storage = memoryStorage();
    srRememberUi("w1", { bookId: "a", stage: "learn" }, storage);
    srRememberUi("w2", { bookId: "b", stage: "bogus" }, storage);
    const prefs = srReadUiPrefs(storage);
    expect(prefs.works.w1).toEqual({ bookId: "a", stage: "learn" });
    expect(prefs.works.w2).toEqual({ bookId: "b", stage: null });
    expect(prefs.last).toEqual({ bookId: "b", stage: null });
    storage.setItem("ws_sr_ui_v1", "{not json");
    expect(srReadUiPrefs(storage)).toEqual({ last: null, works: {} });
  });
});
