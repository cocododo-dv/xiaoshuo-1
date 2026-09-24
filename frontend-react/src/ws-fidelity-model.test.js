// 「像不像」的说法（ws-fidelity-model.js）：一条读数翻成小说作者一眼读懂的话——位次、在不在作者的正常范围、
// 为什么只能参考、越界的地方（后端的白话短语，不给 z 分）、照搬检查只说计数；风格步 / 补丁的决定、成稿中心角标、
// 近期常见偏差按维归组、走势点、出错说法。合成数据，不是任何真书。
import { describe, expect, it } from "vitest";
import {
  fidBadgeView, fidCopyView, fidDimensionGroups, fidErrorInfo, fidExplain, fidFinalsSummary, fidGaps, fidGapsByDimension,
  fidJobView, fidJudgeView, fidPatchView, fidRank, fidReadingLine, fidReadingStates, fidReadingView, fidScoreTone,
  fidStyleStepView, fidTrendPoints, fidUnreliableText, fidVerdict, fidWeakestDims,
} from "./ws-fidelity-model.js";

function reading(over = {}) {
  return {
    reading_id: "r1", percentile: 72.4, within_range: true, reliable: true, max_percentile: 90,
    char_count: 1800, window_count: 40, min_reliable_chars: 600, min_reference_windows: 8,
    emphasized_dimensions: [], excluded_dimensions: [],
    out_of_band: [
      { feature: "fw_modal_per_1k", dimension: "language.vocabulary", dimension_label: "词汇选择", direction: "low", phrase: "语气词（吧、呢、啊、嘛……）比作者少，口气不如作者松", z: -2.8 },
      { feature: "punct_dash_per_1k", dimension: "language.punctuation", dimension_label: "标点节奏", direction: "high", phrase: "破折号比作者多", z: 2.1 },
    ],
    dimension_scores: { "language.vocabulary": 6.1, "scene.dialogue": 8.4 },
    judge: { overall: 7.2, summary: "对白再松一点就更像", dimensions: { "scene.dialogue": { score: 6, note: "对白偏正式" }, "language.rhetoric": { score: 8.5, note: "" } } },
    copy_check: { blocked: false, hits: 0, protected_hits: 0 },
    ...over,
  };
}

describe("一条读数的说法", () => {
  it("位次按 1–100 说，0 也说成第 1 位；在范围内说清前几位算正常", () => {
    expect(fidRank(72.4)).toBe(72);
    expect(fidRank(0)).toBe(1);
    expect(fidRank(100)).toBe(100);
    expect(fidRank(null)).toBeNull();
    const view = fidReadingView(reading());
    expect(view.rankText).toBe("第 72 位");
    expect(view.verdict).toEqual({ key: "within", tone: "ok", label: "在作者的正常范围内" });
    expect(view.explain).toContain("从 1 排到 100（第 1 位最像）");
    expect(view.explain).toContain("这段排在第 72 位，前 90 位都算作者的正常范围");
    expect(fidReadingLine(reading())).toBe("第 72 位 · 在作者的正常范围内");
  });

  it("范围外；位次在前面但重点维越界——说清楚为什么不算在范围内", () => {
    expect(fidVerdict(reading({ within_range: false, percentile: 96 })).label).toBe("超出作者的正常范围");
    expect(fidExplain(reading({ within_range: false, percentile: 96 }))).toContain("前 90 位才算作者的正常范围");
    const emphasized = reading({ within_range: false, percentile: 50, emphasized_dimensions: ["language.vocabulary"] });
    expect(fidExplain(emphasized)).toContain("本来在正常范围里；但你设为「重点」的「词汇选择」越界了");
  });

  it("量不准：太短说字数与门槛，参照太少说片段数；可靠时不说", () => {
    const short = reading({ reliable: false, char_count: 320, unreliable_reason: "too_short" });
    expect(fidVerdict(short)).toMatchObject({ key: "unreliable", tone: "neutral" });
    expect(fidUnreliableText(short)).toBe("这段只有 320 字，太短了，读数只能参考（600 字以上才稳）。");
    expect(fidUnreliableText(reading({ reliable: false, window_count: 5, unreliable_reason: "few_windows" }))).toContain("只有 5 个，至少要 8 个");
    expect(fidUnreliableText(reading())).toBe("");
  });

  it("越界的地方用后端的白话短语、带维度名；从不出现 z 分", () => {
    const gaps = fidGaps(reading());
    expect(gaps.map((g) => g.label)).toEqual(["词汇选择", "标点节奏"]);
    expect(gaps[0].phrase).toContain("语气词");
    expect(JSON.stringify(gaps)).not.toMatch(/-2\.8|2\.1/);
  });

  it("照搬检查只说计数", () => {
    expect(fidCopyView({ blocked: false, hits: 0, protected_hits: 0 })).toEqual({ tone: "ok", text: "没有与参考书原文连续相同的地方，也没用它的专名。" });
    expect(fidCopyView({ blocked: true, hits: 2, protected_hits: 1 })).toEqual({
      tone: "danger",
      text: "有 2 处与参考书原文连续相同——这样的文字不能进正文；另有 1 处用了参考书里的专名（人名、地名等）。",
    });
    // 只用了专名：提醒，不拦（后端的抄袭门只拦原文重合）
    const protectedOnly = fidCopyView({ blocked: false, hits: 0, protected_hits: 3 });
    expect(protectedOnly.tone).toBe("warn");
    expect(protectedOnly.text).toContain("有 3 处用了参考书里的专名");
    expect(protectedOnly.text).toContain("不拦");
    expect(protectedOnly.text).not.toContain("不能进正文");
    expect(fidCopyView(null)).toBeNull();
  });
});

describe("分数与维度", () => {
  it("7 以上像、5 以下差得远", () => {
    expect([fidScoreTone(8), fidScoreTone(6), fidScoreTone(3), fidScoreTone(null)]).toEqual(["ok", "warn", "danger", "neutral"]);
  });

  it("16 维按层：测得只有能统计的几维，评审带说明；重点 / 不学从读数记下的状态来", () => {
    const r = reading({ emphasized_dimensions: ["scene.dialogue"], excluded_dimensions: ["theme.values"] });
    const groups = fidDimensionGroups(r, r.judge, { states: fidReadingStates(r) });
    expect(groups.map((g) => g.label)).toEqual(["语言", "叙事", "场景", "主题"]);
    expect(groups.flatMap((g) => g.rows)).toHaveLength(16);
    const dialogue = groups[2].rows.find((row) => row.dimension === "scene.dialogue");
    expect(dialogue).toMatchObject({ label: "对话写法", measured: 8.4, judged: 6, note: "对白偏正式", state: "emphasize" });
    const rhetoric = groups[0].rows.find((row) => row.dimension === "language.rhetoric");
    expect(rhetoric).toMatchObject({ measured: null, judged: 8.5 });
    expect(groups[3].rows.find((row) => row.dimension === "theme.values").state).toBe("exclude");
  });

  it("评审：没有任何分数就当没有；分最低的几维", () => {
    expect(fidJudgeView({ overall: null, dimensions: {} })).toBeNull();
    expect(fidJudgeView(reading().judge)).toMatchObject({ overall: 7.2, summary: "对白再松一点就更像" });
    expect(fidWeakestDims(reading().judge, 1)).toEqual([{ dimension: "scene.dialogue", label: "对话写法", score: 6, note: "对白偏正式" }]);
  });
});

describe("起草台：风格步与补丁的决定", () => {
  it("首稿在范围内不改；量不准不改；定向修改采用说维度与位次变化；没更像保留首稿", () => {
    expect(fidStyleStepView({ decision: "first_draft_accepted", reason: "within_author_range" })).toEqual({ tone: "ok", text: "首稿已在作者范围内，没有再改" });
    expect(fidStyleStepView({ decision: "first_draft_accepted", reason: "reading_unreliable" }).text).toContain("量不准");
    expect(fidStyleStepView({
      decision: "revision_kept", dimensions: ["scene.dialogue", "language.punctuation"], dimension_labels: ["对话写法", "标点节奏"],
      first_percentile: 95.2, revision_percentile: 61.8,
    }).text).toBe("按 2 个维度（对话写法、标点节奏）定向修改并采用：第 95 位 → 第 62 位");
    expect(fidStyleStepView({ decision: "revision_rejected", reason: "not_closer", dimensions: ["scene.dialogue"] }).text)
      .toBe("按 1 个维度（对话写法）改了一版，但没有更像作者，保留首稿");
    expect(fidStyleStepView({ decision: "revision_rejected", reason: "copy_gate_blocked" }).text).toContain("已丢掉，保留首稿");
    expect(fidStyleStepView(null)).toBeNull();
  });

  it("补丁：退回说原因；保留说没有更远", () => {
    expect(fidPatchView({ decision: "reverted", reason: "judge_worse" })).toEqual({ tone: "warn", text: "补丁没有更像（参考评审分降了），已退回补丁前的稿子" });
    expect(fidPatchView({ decision: "reverted", reason: "distance_worse_without_judge_gain" }).text).toContain("离作者更远");
    expect(fidPatchView({ decision: "kept", reason: "not_worse" }).tone).toBe("ok");
  });
});

describe("成稿中心 / 文风画像 / 走势", () => {
  it("角标：在范围内、超出范围、量不准三种", () => {
    expect(fidBadgeView({ percentile: 41.6, within_range: true, reliable: true })).toMatchObject({ tone: "ok", text: "作者范围内 · 第 42 位" });
    expect(fidBadgeView({ percentile: 96, within_range: false, reliable: true })).toMatchObject({ tone: "warn", text: "超出范围 · 第 96 位" });
    expect(fidBadgeView({ percentile: 50, within_range: true, reliable: false })).toMatchObject({ tone: "neutral", text: "量不准 · 第 50 位" });
    expect(fidBadgeView(null)).toBeNull();
    expect(fidFinalsSummary({ a: { percentile: 30, within_range: true, reliable: true }, b: { percentile: 97, within_range: false, reliable: true }, c: { percentile: 40, within_range: true, reliable: false } }))
      .toEqual({ total: 3, within: 1, text: "3 场终稿里 1 场在作者范围内" });
    expect(fidFinalsSummary({})).toBeNull();
  });

  it("近期常见偏差按维归组；走势点按时间顺序、首稿与终稿分开", () => {
    const byDim = fidGapsByDimension([
      { phrase: "语气词比作者少", dimension: "language.vocabulary", hits: 4, window: 5 },
      { phrase: "没有维度的旧条目" },
    ]);
    expect(byDim).toEqual({ "language.vocabulary": [{ phrase: "语气词比作者少", hits: 4, window: 5 }] });
    const points = fidTrendPoints([
      { reading_id: "a", scene_id: "s1", stage: "first_draft", percentile: 91, within_range: false, reliable: true, created_at: "2026-09-23T01:00:00" },
      { reading_id: "b", scene_id: "s1", stage: "final", percentile: 40, within_range: true, reliable: false },
      { reading_id: "c", stage: "final", percentile: null },
    ]);
    expect(points.map((p) => [p.readingId, p.stage, p.rank, p.within, p.reliable])).toEqual([["a", "first_draft", 91, false, true], ["b", "final", 40, true, false]]);
  });
});

describe("对照检查作业与出错", () => {
  it("进度：排队中 / 各阶段的中文；完成 100%", () => {
    expect(fidJobView({ status: "queued", percent: null })).toMatchObject({ active: true, percent: 0, label: "排队中" });
    expect(fidJobView({ status: "running", phase: "judge", percent: 33.3, elapsed_seconds: 12 })).toMatchObject({ active: true, percent: 33, label: "模型对着原文样例评审", elapsed: 12 });
    expect(fidJobView({ status: "succeeded", percent: 100 })).toMatchObject({ active: false, percent: 100 });
  });

  it("出错按错误码说中文并给下一步；英文原话不给作者看", () => {
    expect(fidErrorInfo({ code: "STYLE_REFERENCE_LLM_REQUIRED", message: "no llm" })).toMatchObject({ action: { type: "settings", label: "去设置模型" } });
    expect(fidErrorInfo({ code: "STYLE_REFERENCE_CHECK_NOT_BOUND" }).action.type).toBe("apply");
    // 作业边界记下的异常串夹几个汉字也不给作者看（与风格参考同一个口径 lib/messages.js）
    expect(fidErrorInfo({ code: "STYLE_REFERENCE_JOB_FAILED", message: "TypeError: 无法读取属性" }).message).toBe("对照检查没有完成，可以重新检查。");
    expect(fidErrorInfo({ code: "STYLE_REFERENCE_CHECK_REFERENCE_EMPTY" }).action.type).toBe("learn");
    const judge = fidErrorInfo({ code: "STYLE_REFERENCE_CHECK_JUDGE_FAILED", message: "judge failed" });
    expect(judge.message).toContain("模型的参考评审没有完成");
    expect(judge.action).toEqual({ type: "retry", label: "重新检查" });
    expect(fidErrorInfo({ code: "WHATEVER", message: "boom in english" }).message).toBe("对照检查没有完成，可以重新检查。");
    expect(fidErrorInfo({ code: "STYLE_REFERENCE_CHECK_TARGET_INVALID", message: "一次最多检查 60000 字；把文字分段检查。" }).message).toBe("一次最多检查 60000 字；把文字分段检查。");
  });

  it("2026-09-24 补的码：取消时已结束、检查对象没给对（英文原话时才用固定文案）、原文范围不对、画像不是在用的版本", () => {
    const infos = [
      fidErrorInfo({ code: "STYLE_REFERENCE_CHECK_NOT_ACTIVE", message: "check not active" }),
      fidErrorInfo({ code: "STYLE_REFERENCE_CHECK_TARGET_INVALID", message: "text and scene_id are mutually exclusive" }),
      fidErrorInfo({ code: "STYLE_REFERENCE_CLOUD_POLICY_INVALID", message: "bad cloud policy" }),
      fidErrorInfo({ code: "STYLE_REFERENCE_PROFILE_NOT_ACTIVE", message: "profile archived" }),
    ];
    expect(infos[0].message).toBe("这次检查已经结束了，不用取消。");
    expect(infos[0].action).toBeNull();
    expect(infos[1].message).toContain("二选一");
    expect(infos[2].message).toContain("原文范围");
    expect(infos[3].message).toContain("不是在用的版本");
    expect(infos[3].action).toEqual({ type: "learn", label: "去学习文风" });
    for (const info of infos) expect(info.message).not.toMatch(/[A-Za-z]/);
  });
});
