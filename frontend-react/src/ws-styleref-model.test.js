// 风格参考 · 纯派生（2026-09-21）：每本书的流水线状态、步骤条状态与落点、指标格式、
// 落点选书与界面偏好。以前书库每本书都写「已导入 · 待抽取」、步骤条按作者点到哪一步打勾、
// 百分比指标的 σ 没换算——这些断言在旧行为下都会失败。
import { describe, expect, it } from "vitest";
import {
  SR_ACTIVITY_WHERE, SR_METRIC_DEFS, SR_STAGE_STATE_LABEL, SR_UI_PREFS_KEY, findShadowedBinding, srBindingActive, srBookPipeline, srChooseProfile, srDimMeta,
  srDraftModeLabel, srFilterBooks, srFormatDuration, srFormatMetric, srFormatPct, srLandingStage, srMetricMeta,
  srFormatWhen, srMetricRows, srParagraphLabel, srPickLandingBook, srReadUiPrefs, srRememberUi, srSortBooks, srSpineColor,
  srStageStates, srStrategyCode, srStrategyName, srSynthErrorMessage,
} from "./ws-styleref-model.js";

const BOOK = { id: "bk1", rawStatus: "ready" };
const deepOf = (over = {}) => ({ loaded: true, run: null, runId: null, profile: null, bindings: [], reports: [], ...over });

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
    dump: () => data,
  };
}

describe("srBookPipeline：一本书现在走到哪一步", () => {
  it("在跑的操作优先：分类 / 抽取 / 合成带百分比", () => {
    expect(srBookPipeline(BOOK, { running: { classify: { percentText: "40%" } } })).toMatchObject({ key: "classifying", label: "分类中 40%", tone: "warn" });
    expect(srBookPipeline(BOOK, { running: { extract: { percentText: "12%" } } })).toMatchObject({ key: "extracting", label: "抽取中 12%" });
    expect(srBookPipeline(BOOK, { running: { synthesize: { percentText: "5%" } } }).key).toBe("synthesizing");
    expect(srBookPipeline({ ...BOOK, rawStatus: "failed" }, {}).key).toBe("classify_failed");
    expect(srBookPipeline({ ...BOOK, rawStatus: "cancelling" }, {}).key).toBe("cancelling");
  });

  it("用于当前作品的书说「当前作品在用」；画像失效时照实说", () => {
    const profiles = [{ profile_id: "p1", status: "active", coverage_json: {} }];
    expect(srBookPipeline(BOOK, { profiles, applied: true })).toMatchObject({ key: "applied", label: "当前作品在用", tone: "ok" });
    const stale = [{ profile_id: "p1", status: "active", coverage_json: { stale: true } }];
    expect(srBookPipeline(BOOK, { profiles: stale, applied: true }).key).toBe("applied_stale");
    expect(srBookPipeline(BOOK, { profiles: stale, applied: false }).key).toBe("profile_stale");
  });

  it("画像状态：已启用 / 待应用；多份画像时取 active 那份", () => {
    expect(srBookPipeline(BOOK, { profiles: [{ status: "draft", coverage_json: {} }], applied: false }).key).toBe("profile_draft");
    expect(srBookPipeline(BOOK, { profiles: [{ status: "draft" }, { status: "active" }, { status: "draft" }], applied: false }).key).toBe("profile_active");
  });

  it("没有画像：有深层数据时按上一次抽取说「待合成 / 待抽取 / 抽取未完成」；否则只说「尚无画像」或「已导入」，不猜", () => {
    expect(srBookPipeline(BOOK, { profiles: [], deep: deepOf({ run: { status: "done" } }) }).key).toBe("ready_to_synthesize");
    expect(srBookPipeline(BOOK, { profiles: [], deep: deepOf() }).key).toBe("ready_to_extract");
    expect(srBookPipeline(BOOK, { profiles: [], deep: deepOf({ run: { status: "failed" } }) }).key).toBe("extract_failed");
    expect(srBookPipeline(BOOK, { profiles: [] }).key).toBe("no_profile");
    // 不再有「已导入 · 待抽取」这种对所有书都一样的说法
    expect(srBookPipeline(BOOK, { profiles: null })).toMatchObject({ key: "imported", label: "已导入" });
  });

  it("深层数据里的画像比书库清单新（刚合成完）：以深层数据为准", () => {
    const deep = deepOf({ run: { status: "done" }, profile: { status: "draft", coverage_json: {} } });
    expect(srBookPipeline(BOOK, { profiles: [], deep }).key).toBe("profile_draft");
  });
});

describe("srStageStates / srLandingStage：步骤条来自真实数据", () => {
  it("没抽取过的书：概览完成，矩阵未开始，画像 / 回测 / 应用等前一步——不再按位置打勾", () => {
    const s = srStageStates(BOOK, deepOf());
    expect(s).toEqual({ overview: "done", matrix: "todo", profile: "blocked", validation: "blocked", apply: "blocked" });
    expect(srLandingStage(s)).toBe("matrix");
  });

  it("分类没完成：概览需处理、矩阵等前一步，落在概览", () => {
    const s = srStageStates({ ...BOOK, rawStatus: "failed" }, deepOf());
    expect(s.overview).toBe("attention");
    expect(s.matrix).toBe("blocked");
    expect(srLandingStage(s)).toBe("overview");
    expect(srStageStates({ ...BOOK, rawStatus: "ingesting" }, null).overview).toBe("running");
  });

  it("抽完未合成落在画像；有画像没绑定落在应用（回测是可选的一步，不作落点）", () => {
    const extracted = srStageStates(BOOK, deepOf({ run: { status: "done" }, runId: "r1" }));
    expect(extracted).toMatchObject({ matrix: "done", profile: "todo" });
    expect(srLandingStage(extracted)).toBe("profile");
    const synthesized = srStageStates(BOOK, deepOf({ run: { status: "done" }, runId: "r1", profile: { run_id: "r1", status: "draft", coverage_json: {} } }));
    expect(synthesized).toMatchObject({ profile: "done", validation: "todo", apply: "todo" });
    expect(srLandingStage(synthesized)).toBe("apply");
  });

  it("画像失效或有新 run：画像需处理；有回测报告与 active 绑定：回测、应用完成；用于当前作品直接落在应用", () => {
    const stale = srStageStates(BOOK, deepOf({ run: { status: "done" }, runId: "r2", profile: { run_id: "r1", status: "active", coverage_json: {} } }));
    expect(stale.profile).toBe("attention");
    expect(srLandingStage(stale)).toBe("profile");
    expect(srLandingStage(stale, { applied: true })).toBe("apply");
    const full = srStageStates(BOOK, deepOf({
      run: { status: "done" }, runId: "r1", profile: { run_id: "r1", status: "active", coverage_json: {} },
      reports: [{ verdict: "pass", status: "done" }], bindings: [{ binding_id: "b1", status: "active" }],
    }));
    expect(full).toEqual({ overview: "done", matrix: "done", profile: "done", validation: "done", apply: "done" });
    expect(srStageStates(BOOK, deepOf({ run: { status: "done" }, runId: "r1", profile: { run_id: "r1" }, bindings: [{ status: "revoked" }] })).apply).toBe("todo");
  });

  it("深层数据还没读到：后四步是 unknown，不说「未开始 / 等前一步」（没读到不等于没做）", () => {
    for (const deep of [null, { loaded: false }]) {
      const s = srStageStates(BOOK, deep);
      expect(s.overview).toBe("done");
      expect([s.matrix, s.profile, s.validation, s.apply]).toEqual(["unknown", "unknown", "unknown", "unknown"]);
      expect(Object.values(s)).not.toContain("todo");
      expect(Object.values(s)).not.toContain("blocked");
    }
    expect(SR_STAGE_STATE_LABEL.unknown).toBe("");
    // 书本身的状态已经说明问题的，照说：分类没完成 → 矩阵等前一步；在跑的照样是进行中
    expect(srStageStates({ ...BOOK, rawStatus: "failed" }, null).matrix).toBe("blocked");
    expect(srStageStates(BOOK, null, { extract: { percentText: "3%" } }).matrix).toBe("running");
    expect(srStageStates(BOOK, null, { synthesize: { percentText: "3%" } }).profile).toBe("running");
  });

  it("正在抽取 / 合成：对应一步是进行中", () => {
    expect(srStageStates(BOOK, deepOf(), { extract: { percentText: "3%" } }).matrix).toBe("running");
    expect(srStageStates(BOOK, deepOf({ run: { status: "done" } }), { synthesize: { percentText: "1%" } }).profile).toBe("running");
  });
});

describe("指标格式：百分比指标的波动也换算成百分点", () => {
  it("srFormatMetric：23% 的波动是 ±9.7%，不是 σ 0.1", () => {
    const pct = SR_METRIC_DEFS.find((d) => d.key === "short_sentence_ratio");
    expect(srFormatMetric(pct, { mean: 0.2271, std: 0.0974 })).toMatchObject({ value: "23%", spread: "±9.7%", unit: "" });
    const plain = SR_METRIC_DEFS.find((d) => d.key === "avg_sentence_length");
    expect(srFormatMetric(plain, { mean: 28.847, std: 6.876 })).toMatchObject({ value: "28.8", spread: "±6.9", unit: "字" });
    expect(srFormatMetric(plain, { mean: null })).toBeNull();
    expect(srFormatPct(0.004)).toBe("0.4%");
    expect(srFormatPct(0)).toBe("0%");
    expect(srFormatPct(0.58)).toBe("58%");
  });

  it("srMetricRows：按固定顺序取有数据的项，总览与画像用同一组", () => {
    const rows = srMetricRows({ dialogue_ratio: { mean: 0.5, std: 0.1 }, avg_sentence_length: { mean: 20, std: 3 }, unknown_metric: { mean: 1 } });
    expect(rows.map((r) => r.key)).toEqual(["avg_sentence_length", "dialogue_ratio"]);
    expect(srMetricRows(null)).toEqual([]);
  });
});

describe("小工具", () => {
  it("srParagraphLabel：段落 id 末尾序号 → 第 N 段；认不出返回 null", () => {
    expect(srParagraphLabel("sr_para_abcd1234_1979")).toBe("第 1,980 段");
    expect(srParagraphLabel("para_x")).toBeNull();
    expect(srParagraphLabel(null)).toBeNull();
  });

  it("策略名：中文名 + 简写徽标", () => {
    expect(srStrategyCode("mixed")).toBe("A+B");
    expect(srStrategyName("mixed")).toBe("规则 + 样例");
    expect(srStrategyCode("A")).toBe("A");
    expect(srStrategyName("C")).toBe("相近片段");
  });

  it("srSortBooks：用于当前作品的书置顶，其余保持原序；srFilterBooks 按书名 / 作者", () => {
    const books = [{ id: "a", title: "甲书" }, { id: "b", title: "乙书", author: "某作者" }, { id: "c", title: "丙书" }];
    expect(srSortBooks(books, new Set(["c"])).map((b) => b.id)).toEqual(["c", "a", "b"]);
    expect(srSortBooks(books, null).map((b) => b.id)).toEqual(["a", "b", "c"]);
    expect(srFilterBooks(books, "乙").map((b) => b.id)).toEqual(["b"]);
    expect(srFilterBooks(books, "某作者").map((b) => b.id)).toEqual(["b"]);
    expect(srFilterBooks(books, " ")).toBe(books);
  });

  it("srSpineColor：同一本书永远同一个颜色", () => {
    expect(srSpineColor("sr_book_1")).toBe(srSpineColor("sr_book_1"));
    expect(["crimson", "gold", "slate", "sage"]).toContain(srSpineColor("x"));
  });
});

describe("落点选书与界面偏好 ws_sr_ui_v1", () => {
  const books = [{ id: "a" }, { id: "b" }, { id: "c" }];

  it("本次会话刚看的 → 当前作品在用的书 → 这部作品上次打开的书 → 上次打开的书 → 第一本", () => {
    const prefs = { last: { bookId: "c", stage: "matrix" }, works: { w1: { bookId: "a", stage: "profile" } } };
    // 作者上次瞄过一眼 a（本机记录），但这部作品在用的是 b：进来落在 b，不是 a
    expect(srPickLandingBook(books, { prefs, workId: "w1", appliedBookIds: new Set(["b"]) })).toMatchObject({ bookId: "b", stage: null, source: "applied" });
    // 本次会话里刚在看 a（离开页面又回来）：接着看 a
    expect(srPickLandingBook(books, { prefs, workId: "w1", appliedBookIds: new Set(["b"]), session: { bookId: "a", stage: "matrix" } }))
      .toMatchObject({ bookId: "a", stage: "matrix", source: "session" });
    // 没有在用的书：才轮到本机记录（先本作品，再全局）
    expect(srPickLandingBook(books, { prefs, workId: "w1", appliedBookIds: new Set() })).toMatchObject({ bookId: "a", stage: "profile", source: "work" });
    expect(srPickLandingBook(books, { prefs, workId: "w2", appliedBookIds: new Set() })).toMatchObject({ bookId: "c", stage: "matrix", source: "last" });
    expect(srPickLandingBook(books, { prefs: { last: { bookId: "gone" }, works: {} }, workId: "w2" })).toMatchObject({ bookId: "a", source: "first" });
    // 会话记录指向已删除的书：不算数
    expect(srPickLandingBook(books, { prefs, workId: "w1", appliedBookIds: new Set(["b"]), session: { bookId: "gone" } }).bookId).toBe("b");
    expect(srPickLandingBook([], {})).toBeNull();
  });

  it("还不知道当前作品在用哪本、又没有本次会话的记录：先等（返回 null），不先落到别处再跳", () => {
    expect(srPickLandingBook(books, { prefs: { last: { bookId: "c" }, works: {} }, workId: "w2", metaSettled: false })).toBeNull();
    // 本机记录不够：在用的书优先于它，得等附加事实
    expect(srPickLandingBook(books, { prefs: { last: null, works: { w2: { bookId: "b" } } }, workId: "w2", metaSettled: false })).toBeNull();
    // 有本次会话的记录就不用等
    expect(srPickLandingBook(books, { prefs: { last: null, works: {} }, workId: "w2", metaSettled: false, session: { bookId: "b" } }).bookId).toBe("b");
  });

  it("srRememberUi / srReadUiPrefs：按作品记住书与步骤；存储读写抛错时不报错", () => {
    const store = memoryStorage();
    srRememberUi("w1", { bookId: "a", stage: "matrix" }, store);
    srRememberUi("w2", { bookId: "b", stage: null }, store);
    const prefs = srReadUiPrefs(store);
    expect(prefs.works).toEqual({ w1: { bookId: "a", stage: "matrix" }, w2: { bookId: "b", stage: null } });
    expect(prefs.last).toEqual({ bookId: "b", stage: null });
    expect(JSON.parse(store.dump().get(SR_UI_PREFS_KEY)).last.bookId).toBe("b");

    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    expect(srReadUiPrefs(broken)).toEqual({ last: null, works: {} });
    expect(() => srRememberUi("w1", { bookId: "a" }, broken)).not.toThrow();
    const garbage = memoryStorage();
    garbage.setItem(SR_UI_PREFS_KEY, "{not json");
    expect(srReadUiPrefs(garbage)).toEqual({ last: null, works: {} });
  });
});

describe("拆分后收进来的共用说法（2026-09-21 第二阶段）", () => {
  it("srDimMeta：子维度路径 → 层 / 名；认不出原样返回路径", () => {
    expect(srDimMeta("scene.dialogue")).toEqual({ abbr: "景", layer: "场景层", name: "对话写法" });
    expect(srDimMeta("nope.x")).toEqual({ abbr: "·", layer: "", name: "nope.x" });
  });

  it("指标名只有一份：总览的 8 项取自全表，回测逐项也用它（以前回测写「短句率」、总览写「短句占比」）", () => {
    for (const def of SR_METRIC_DEFS) expect(srMetricMeta(def.key).name).toBe(def.name);
    expect(srMetricMeta("short_sentence_ratio")).toMatchObject({ name: "短句占比", pct: true });
    expect(srMetricMeta("sentence_length_std").name).toBe("句长波动");
    expect(srMetricMeta("mystery_metric")).toEqual({ name: "mystery_metric", unit: "" });
  });

  it("绑定是否生效：缺 status 视为生效；同作用域遮蔽只看生效的绑定", () => {
    expect(srBindingActive({})).toBe(true);
    expect(srBindingActive({ status: "revoked" })).toBe(false);
    expect(srBindingActive(null)).toBe(false);
    const bindings = [
      { binding_id: "old", scope: "project", scope_ref_id: "w1", status: "revoked" },
      { binding_id: "cur", scope: "project", scope_ref_id: "w1" },
    ];
    expect(findShadowedBinding(bindings, "project", "w1").binding_id).toBe("cur");
    expect(findShadowedBinding(bindings.slice(0, 1), "project", "w1")).toBeNull();
    // 步骤条「注入应用」用同一个判断
    expect(srStageStates(BOOK, deepOf({ run: { status: "done" }, runId: "r1", profile: { run_id: "r1" }, bindings: [{}] })).apply).toBe("done");
  });

  it("srChooseProfile：优先 active，否则最新一份；store 读深层数据与书库徽标同一条规则", () => {
    expect(srChooseProfile([{ profile_id: "a", status: "draft" }, { profile_id: "b", status: "active" }, { profile_id: "c", status: "draft" }]).profile_id).toBe("b");
    expect(srChooseProfile([{ profile_id: "a", status: "draft" }, { profile_id: "c", status: "draft" }]).profile_id).toBe("c");
    expect(srChooseProfile([])).toBeNull();
  });

  it("合成失败文案：按错误码说清下一步；起草方式标签缺省是「作者手笔直起」；时长 m:ss", () => {
    expect(srSynthErrorMessage({ code: "STYLE_REFERENCE_LLM_REQUIRED" })).toContain("先接入模型");
    expect(srSynthErrorMessage({ code: "STYLE_REFERENCE_SYNTHESIZE_FAILED", details: { reason_code: "budget_unfit" } })).toBe("合成失败：观察太多，装不进合成预算：回维度矩阵驳回一部分后重试。");
    expect(srSynthErrorMessage(new Error("x"))).toBe("合成失败：x");
    expect(srDraftModeLabel(undefined)).toBe("作者手笔直起");
    expect(srDraftModeLabel("neutral_first")).toBe("中性稿再上风格");
    expect(srFormatDuration(200)).toBe("3:20");
    // 「参考书活动」在哪：两种宽度下都在「参考书库」里（≤1280 左栏收进页头的「参考书库」抽屉）
    expect(SR_ACTIVITY_WHERE).toContain("「参考书库」");
    expect(srSynthErrorMessage({ code: "STYLE_REFERENCE_SYNTHESIS_ALREADY_ACTIVE" })).toContain(SR_ACTIVITY_WHERE);
    expect(srFormatWhen("2026-09-20T14:05:00")).toBe("9 月 20 日 14:05");
    expect(srFormatWhen("nope")).toBeNull();
    expect(srFormatWhen(null)).toBeNull();
    expect(srFormatDuration(-5)).toBe("0:00");
  });
});
