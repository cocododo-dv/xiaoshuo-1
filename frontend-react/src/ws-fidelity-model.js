/* ==========================================================
   「像不像」—— 说法与纯派生（2026-09-23 风格参考 v3 · P6b）
   ----------------------------------------------------------
   一条读数（后端 readings.reading_payload）说的是：拿作者自己书里的段落当尺子，这段文字排在哪。
   percentile 0 = 最像作者，100 = 最不像；within_range = 在作者的正常范围内（前 max_percentile 位，且设为
   「重点」的维没有越界）；reliable = 文字够长、参考书能比的片段够多。这里把它翻成小说作者一眼读懂的话：
   「第 72 位 / 100」+「在作者的正常范围内」+ 一句解释；越界的地方用后端给的白话短语，从不给作者看 z 分。
   · 风格参考的「对照检查」、起草台的「像不像」、成稿中心的角标、文风画像的逐维平均都从这里取说法；
   · 维度名与层名从 ws-labels.js 取（一张词表）。
   不依赖 React，不读 store，不写 window，可单测。
   ========================================================== */
import { STYLE_DIMENSIONS, STYLE_LAYER_LABELS, STYLE_LAYER_ORDER, styleDimensionLabel, styleLayerOf } from "./ws-labels.js";

const DEFAULT_MAX_PERCENTILE = 90;
const DEFAULT_MIN_RELIABLE_CHARS = 600;
const DEFAULT_MIN_REFERENCE_WINDOWS = 8;

function num(value) {
  const n = Number(value);
  return value == null || value === "" || !Number.isFinite(n) ? null : n;
}

/* 百分位 → 位次 1–100（0 也说成第 1 位：比作者自己的每一段都更像作者） */
export function fidRank(percentile) {
  const p = num(percentile);
  if (p == null) return null;
  return Math.max(1, Math.min(100, Math.round(p)));
}

export function fidRankText(percentile) {
  const rank = fidRank(percentile);
  return rank == null ? "—" : `第 ${rank} 位`;
}

function listText(items) {
  return items.map((x) => `「${x}」`).join("");
}

/* 这条读数算不算在作者的正常范围里：{ tone, label, key } */
export function fidVerdict(reading) {
  if (!reading) return null;
  if (reading.reliable === false) return { key: "unreliable", tone: "neutral", label: "量不准" };
  if (reading.within_range) return { key: "within", tone: "ok", label: "在作者的正常范围内" };
  return { key: "outside", tone: "warn", label: "超出作者的正常范围" };
}

/* 重点维里越界的那几维（中文名） */
function emphasizedGaps(reading) {
  const emphasized = new Set(Array.isArray(reading.emphasized_dimensions) ? reading.emphasized_dimensions : []);
  if (!emphasized.size) return [];
  const labels = [];
  (reading.out_of_band || []).forEach((item) => {
    if (!item || !emphasized.has(item.dimension)) return;
    const label = item.dimension_label || styleDimensionLabel(item.dimension);
    if (label && !labels.includes(label)) labels.push(label);
  });
  return labels;
}

/* 读数为什么只能参考 */
export function fidUnreliableText(reading) {
  if (!reading || reading.reliable !== false) return "";
  const minChars = num(reading.min_reliable_chars) || DEFAULT_MIN_RELIABLE_CHARS;
  const minWindows = num(reading.min_reference_windows) || DEFAULT_MIN_REFERENCE_WINDOWS;
  const chars = num(reading.char_count) || 0;
  const reason = reading.unreliable_reason || (chars < minChars ? "too_short" : "few_windows");
  if (reason === "too_short") {
    return `这段只有 ${chars.toLocaleString("zh-CN")} 字，太短了，读数只能参考（${minChars} 字以上才稳）。`;
  }
  const windows = num(reading.window_count);
  return `参考书里能拿来比的片段太少${windows != null ? `（只有 ${windows} 个，至少要 ${minWindows} 个）` : ""}，读数只能参考。`;
}

/* 一句解释：尺子是什么、这段排在哪、范围到哪 */
export function fidExplain(reading) {
  if (!reading) return "";
  const rank = fidRank(reading.percentile);
  if (rank == null) return "";
  const threshold = Math.round(num(reading.max_percentile) || DEFAULT_MAX_PERCENTILE);
  const ruler = "把这位作者自己书里的段落，按像不像作者本人从 1 排到 100（第 1 位最像）";
  if (reading.within_range) return `${ruler}：这段排在第 ${rank} 位，前 ${threshold} 位都算作者的正常范围。`;
  const gaps = emphasizedGaps(reading);
  if (rank <= threshold && gaps.length) {
    return `${ruler}：这段排在第 ${rank} 位，本来在正常范围里；但你设为「重点」的${listText(gaps)}越界了，所以不算在范围内。`;
  }
  return `${ruler}：这段排在第 ${rank} 位，前 ${threshold} 位才算作者的正常范围。`;
}

/* 越界的地方：[{ key, dimension, label, phrase }]（后端已按偏离大小排好，这里只去重） */
export function fidGaps(reading) {
  const out = [];
  const seen = new Set();
  ((reading && reading.out_of_band) || []).forEach((item) => {
    if (!item || !item.phrase) return;
    const key = `${item.feature || ""}:${item.direction || ""}:${item.phrase}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({
      key,
      dimension: item.dimension || "",
      label: item.dimension_label || (item.dimension ? styleDimensionLabel(item.dimension) : ""),
      phrase: String(item.phrase),
    });
  });
  return out;
}

/* 照搬检查一句话（只有计数，从不给参考原文） */
export function fidCopyView(copy) {
  if (!copy) return null;
  const hits = num(copy.hits) || 0;
  const protectedHits = num(copy.protected_hits) || 0;
  if (copy.blocked || hits || protectedHits) {
    const parts = [];
    if (hits) parts.push(`${hits} 处与参考书原文连续相同`);
    if (protectedHits) parts.push(`${protectedHits} 处用了参考书里的专名（人名、地名等）`);
    return { tone: "danger", text: `${parts.length ? `有 ${parts.join("、")}` : "有照搬参考书原文的地方"}——这样的文字不能进正文。` };
  }
  return { tone: "ok", text: "没有与参考书原文连续相同的地方，也没用它的专名。" };
}

/* 一条读数给界面的全部说法 */
export function fidReadingView(reading) {
  if (!reading) return null;
  return {
    rank: fidRank(reading.percentile),
    rankText: fidRankText(reading.percentile),
    verdict: fidVerdict(reading),
    explain: fidExplain(reading),
    unreliable: fidUnreliableText(reading),
    gaps: fidGaps(reading),
    copy: fidCopyView(reading.copy_check),
    chars: num(reading.char_count) || 0,
  };
}

/* 一行短话：「第 72 位 · 在作者的正常范围内」 */
export function fidReadingLine(reading) {
  if (!reading) return "";
  const verdict = fidVerdict(reading);
  return `${fidRankText(reading.percentile)} · ${verdict ? verdict.label : ""}`;
}

/* 读数记下的维度状态（重点 / 不学）→ { dim: "emphasize" | "exclude" }（按维的表据此标出） */
export function fidReadingStates(reading) {
  const states = {};
  ((reading && reading.emphasized_dimensions) || []).forEach((dim) => { states[dim] = "emphasize"; });
  ((reading && reading.excluded_dimensions) || []).forEach((dim) => { states[dim] = "exclude"; });
  return states;
}

/* ---------- 分数（0–10） ---------- */

export function fidScoreTone(score) {
  const s = num(score);
  if (s == null) return "neutral";
  if (s >= 7) return "ok";
  if (s >= 5) return "warn";
  return "danger";
}

export function fidScoreWord(score) {
  const s = num(score);
  if (s == null) return "没有分数";
  if (s >= 7) return "像";
  if (s >= 5) return "有些差距";
  return "差得远";
}

export function fidScoreText(score) {
  const s = num(score);
  return s == null ? "—" : (Math.round(s * 10) / 10).toFixed(1);
}

/* 评审（软质检的参考评审 / 对照检查）：{ overall, summary, dims: { dim: { score, note } } } 或 null */
export function fidJudgeView(judge) {
  if (!judge || typeof judge !== "object") return null;
  const dims = {};
  Object.entries(judge.dimensions || {}).forEach(([dim, entry]) => {
    const score = num(entry && typeof entry === "object" ? entry.score : entry);
    if (score == null) return;
    dims[dim] = { score, note: entry && typeof entry === "object" ? String(entry.note || "") : "" };
  });
  const overall = num(judge.overall);
  if (overall == null && !Object.keys(dims).length) return null;
  return { overall, summary: String(judge.summary || ""), dims };
}

/* 评审里分最低的几维（起草台上的简表） */
export function fidWeakestDims(judge, limit = 3) {
  const view = fidJudgeView(judge);
  if (!view) return [];
  return Object.entries(view.dims)
    .map(([dimension, entry]) => ({ dimension, label: styleDimensionLabel(dimension), ...entry }))
    .sort((a, b) => a.score - b.score || STYLE_DIMENSIONS.indexOf(a.dimension) - STYLE_DIMENSIONS.indexOf(b.dimension))
    .slice(0, limit);
}

/* 16 维按层：[{ layer, label, rows: [{ dimension, label, measured, judged, note, state }] }]
   measured：确定性读数（只有能按字数统计的几维有）；judged：评审分；state：给这部作品设的维度状态 */
export function fidDimensionGroups(reading, judge, { states = null } = {}) {
  const measured = (reading && reading.dimension_scores) || {};
  const view = fidJudgeView(judge);
  return STYLE_LAYER_ORDER.map((layer) => ({
    layer,
    label: STYLE_LAYER_LABELS[layer],
    rows: STYLE_DIMENSIONS.filter((dim) => styleLayerOf(dim) === layer).map((dim) => ({
      dimension: dim,
      label: styleDimensionLabel(dim),
      measured: num(measured[dim]),
      judged: view && view.dims[dim] ? view.dims[dim].score : null,
      note: view && view.dims[dim] ? view.dims[dim].note : "",
      state: (states && states[dim]) || "normal",
    })),
  }));
}

/* ---------- 起草台：风格步与补丁的决定 ---------- */

const STEP_TEXT = {
  within_author_range: { tone: "ok", text: "首稿已在作者范围内，没有再改" },
  reading_unreliable: { tone: "neutral", text: "首稿太短（或参考书能比的片段太少），量不准，没有再改" },
  reading_unavailable: { tone: "neutral", text: "参考书还没有量像不像的尺子，首稿没有再改" },
  best_of_n_candidate: { tone: "neutral", text: "首稿作为候选之一，和修改稿一起按像不像排序" },
  revision_template_missing: { tone: "warn", text: "首稿和作者差得明显，但「定向修改」的提示词模板还没同步，保留首稿" },
};

const REJECT_TEXT = {
  copy_gate_blocked: "改出来的那一版有与参考书原文连续相同的地方（或用了它的专名），已丢掉，保留首稿",
  base_safety_failed: "改出来的那一版丢了必写的内容或长度不对，保留首稿",
  revision_reading_unavailable: "改出来的那一版量不出像不像，保留首稿",
};

function dimsText(step) {
  const labels = (step.dimension_labels && step.dimension_labels.length ? step.dimension_labels : (step.dimensions || []).map(styleDimensionLabel))
    .filter(Boolean);
  if (!labels.length) return "";
  return `按 ${labels.length} 个维度（${labels.join("、")}）`;
}

/* 风格步做了什么：{ tone, text } 或 null */
export function fidStyleStepView(step) {
  if (!step || !step.decision) return null;
  if (step.decision === "first_draft_accepted") {
    return STEP_TEXT[step.reason] || { tone: "neutral", text: "风格步保留了首稿" };
  }
  const dims = dimsText(step);
  if (step.decision === "revision_kept") {
    const from = fidRank(step.first_percentile);
    const to = fidRank(step.revision_percentile);
    const moved = from != null && to != null ? `：第 ${from} 位 → 第 ${to} 位` : "";
    return { tone: "ok", text: `${dims || "按量出来的差距"}定向修改并采用${moved}` };
  }
  if (step.decision === "revision_rejected") {
    if (step.reason === "not_closer") return { tone: "neutral", text: `${dims || "按量出来的差距"}改了一版，但没有更像作者，保留首稿` };
    return { tone: "neutral", text: REJECT_TEXT[step.reason] || "修改没有采用，保留首稿" };
  }
  return null;
}

/* 软质检的补丁留没留：{ tone, text } 或 null */
export function fidPatchView(patch) {
  if (!patch || !patch.decision) return null;
  if (patch.decision === "reverted") {
    return {
      tone: "warn",
      text: patch.reason === "judge_worse"
        ? "补丁没有更像（参考评审分降了），已退回补丁前的稿子"
        : "补丁没有更像（离作者更远、评审分也没提高），已退回补丁前的稿子",
    };
  }
  if (patch.reason === "no_comparable_evidence") return { tone: "neutral", text: "软质检的补丁保留（没有可比的读数）" };
  return { tone: "ok", text: "软质检的补丁保留（没有离作者更远）" };
}

/* ---------- 成稿中心：按场的角标 ---------- */

/* 一场最新的终稿读数（作品汇总的 scene_finals[sceneId]）→ { tone, text, title } 或 null */
export function fidBadgeView(final, { maxPercentile = DEFAULT_MAX_PERCENTILE } = {}) {
  if (!final || fidRank(final.percentile) == null) return null;
  const rank = fidRank(final.percentile);
  const ruler = `终稿像不像这位参考作者：把作者自己书里的段落从最像到最不像排成 100 位，这一场排第 ${rank} 位`;
  if (final.reliable === false) {
    return { tone: "neutral", text: `量不准 · 第 ${rank} 位`, title: `${ruler}；这一场太短（或参考书能比的片段太少），只能参考。` };
  }
  if (final.within_range) {
    return { tone: "ok", text: `作者范围内 · 第 ${rank} 位`, title: `${ruler}，在作者的正常范围内（前 ${maxPercentile} 位）。` };
  }
  return { tone: "warn", text: `超出范围 · 第 ${rank} 位`, title: `${ruler}，超出作者的正常范围。` };
}

/* 作品汇总的一句：「12 场终稿里 9 场在作者范围内」 */
export function fidFinalsSummary(sceneFinals) {
  const finals = Object.values(sceneFinals || {}).filter((f) => f && fidRank(f.percentile) != null);
  if (!finals.length) return null;
  const within = finals.filter((f) => f.reliable !== false && f.within_range).length;
  return { total: finals.length, within, text: `${finals.length} 场终稿里 ${within} 场在作者范围内` };
}

/* ---------- 文风画像：作品在各维上的平均 / 近期常见偏差 ---------- */

/* recent_gap_details → { dim: [{ phrase, hits, window }] } */
export function fidGapsByDimension(details) {
  const out = {};
  (Array.isArray(details) ? details : []).forEach((item) => {
    if (!item || !item.dimension || !item.phrase) return;
    (out[item.dimension] = out[item.dimension] || []).push({
      phrase: String(item.phrase),
      hits: num(item.hits) || 0,
      window: num(item.window) || 0,
    });
  });
  return out;
}

/* 走势：trend 行 → [{ index, readingId, sceneId, stage, rank, within, reliable, maxPercentile, at }]（时间顺序） */
export function fidTrendPoints(trend) {
  return (Array.isArray(trend) ? trend : [])
    .filter((row) => row && fidRank(row.percentile) != null)
    .map((row, index) => ({
      index,
      readingId: row.reading_id || String(index),
      sceneId: row.scene_id || null,
      stage: row.stage === "first_draft" ? "first_draft" : "final",
      rank: fidRank(row.percentile),
      within: !!row.within_range,
      reliable: row.reliable !== false,
      maxPercentile: num(row.max_percentile),
      at: row.created_at || null,
    }));
}

export const FID_STAGE_LABELS = { first_draft: "首稿", revision: "修改稿", patched: "补丁后", final: "终稿", manual: "对照检查" };

/* ---------- 对照检查作业 ---------- */

const CHECK_PHASE_LABELS = { queued: "排队中", measure: "量像不像", judge: "模型对着原文样例评审", record: "记下结果", done: "完成" };

export function fidJobActive(job) {
  return !!job && (job.status === "queued" || job.status === "running");
}

/* 作业条目 → { active, percent, label, elapsed } */
export function fidJobView(job) {
  if (!job) return { active: false, percent: 0, label: "", elapsed: null };
  const active = fidJobActive(job);
  let percent = 0;
  if (job.status === "succeeded") percent = 100;
  else if (num(job.percent) != null) percent = Math.max(0, Math.min(99, Math.round(num(job.percent))));
  const label = job.status === "queued" ? "排队中" : (CHECK_PHASE_LABELS[job.phase] || job.phase_label || (active ? "进行中" : ""));
  return { active, percent, label, elapsed: num(job.elapsed_seconds) };
}

/* ---------- 出错：作者读得懂的一句话 + 下一步 ---------- */

const ERROR_TEXT = {
  STYLE_REFERENCE_LLM_REQUIRED: "对照检查要由模型对着原文样例评审，但还没有接入可用的模型。",
  STYLE_REFERENCE_CHECK_NOT_BOUND: "这一场（这部作品）还没有用上参考书的文风，没有可以对照的参考。",
  STYLE_REFERENCE_CHECK_NO_TEXT: "这一场还没有正文可以检查（没有终稿、草稿，也没有你写的稿子）。",
  STYLE_REFERENCE_CHECK_NO_REFERENCE: "参考书还没有可以拿来比的片段：等段落分类做完、学完文风之后再查。",
  STYLE_REFERENCE_CHECK_REFERENCE_EMPTY: "这份文风画像拿不出可以对照的参考（没有原文片段，也没有文风卡）：先学习文风。",
  STYLE_REFERENCE_CHECK_JUDGE_FAILED: "模型的参考评审没有完成（调用失败或没给出分数），可以重新检查。",
  STYLE_REFERENCE_CHECK_CONFIG_MISSING: "这台机器还没有对照检查用的评审提示词：同步提示词模板（sync_prompt_templates --execute）后再试。",
  STYLE_REFERENCE_CHECK_NOT_FOUND: "这次检查的记录已经不在了，重新检查一次。",
  STYLE_REFERENCE_CLOUD_POLICY_BLOCKED: "这本书设为「仅本机模型」，但评审用的是云端模型：换成本机模型再查。",
  STYLE_REFERENCE_PROFILE_NOT_FOUND: "这份文风画像已经不在了。",
  STYLE_REFERENCE_JOB_CANCELLED: "这次检查被取消了。",
  SCENE_NOT_FOUND: "这一场已经不在目录里了。",
  NETWORK_ERROR: "连不上后端：检查后端是否在运行后重试。",
  REQUEST_TIMEOUT: "等太久了没有回音，稍后再试。",
};

const RETRYABLE = new Set([
  "STYLE_REFERENCE_CHECK_JUDGE_FAILED", "STYLE_REFERENCE_CHECK_NOT_FOUND", "STYLE_REFERENCE_JOB_CANCELLED",
  "NETWORK_ERROR", "REQUEST_TIMEOUT",
]);

const CJK = /[㐀-鿿]/;

/* 出错 → { code, message, action }；action：{ type: "settings" | "learn" | "apply" | "retry", label } 或 null */
export function fidErrorInfo(error, fallback = "对照检查没有完成，可以重新检查。") {
  const code = String((error && error.code) || "");
  const details = (error && error.details) || {};
  const serverMessage = String((error && error.message) || "");
  const message = ERROR_TEXT[code] || (CJK.test(serverMessage) ? serverMessage : fallback);
  let action = null;
  if (code === "STYLE_REFERENCE_LLM_REQUIRED" || code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED") action = { type: "settings", label: "去设置模型" };
  else if (code === "STYLE_REFERENCE_CHECK_NOT_BOUND") action = { type: "apply", label: "去用于作品" };
  else if (code === "STYLE_REFERENCE_CHECK_NO_REFERENCE" || code === "STYLE_REFERENCE_CHECK_REFERENCE_EMPTY") action = { type: "learn", label: "去学习文风" };
  else if (RETRYABLE.has(code) || details.retryable === true || (error && error.retryable === true) || !code) action = { type: "retry", label: "重新检查" };
  return { code, message, action };
}
