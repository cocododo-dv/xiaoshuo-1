/* ==========================================================
   风格参考 · 词汇与纯派生（2026-09-21 前端重构）
   几个 stage 共用的说法（16 个子维度、段落类型、置信度、起草方式、作用域、指标名）
   与判断（书库每本书「现在走到哪一步」、流水线五步的状态与落点、注入维度选项、
   是否该重新合成、同作用域遮蔽、最新 run、合成失败文案、数字格式、界面偏好 ws_sr_ui_v1）。
   不依赖 React，不 import 任何模块，不写 window，可单测；store 与各 stage 都从这里取。
   ========================================================== */

/* ---- 4 层 × 4 子维 = 16 个子维度 ----
   只保留 id / 展示名（与后端 dimensions.SubDimension 一一对应）。置信度、计数、
   「语料不足」等全部来自后端 deep 数据（dimCounts / profile_json.sub_dimensions /
   stats_json.input_assessment），这里不硬编码任何 conf / skip。 */
export const SR_LAYERS = [
  {
    id: "language", name: "语言层", abbr: "语",
    subs: [
      { id: "sentence_structure", name: "句式结构" },
      { id: "vocabulary",         name: "词汇选择" },
      { id: "rhetoric",           name: "修辞手法" },
      { id: "punctuation",        name: "标点节奏" },
    ],
  },
  {
    id: "narrative", name: "叙事层", abbr: "叙",
    subs: [
      { id: "perspective",         name: "叙事视角" },
      { id: "pacing",              name: "节奏控制" },
      { id: "time_handling",       name: "时间处理" },
      { id: "information_density", name: "信息密度" },
    ],
  },
  {
    id: "scene", name: "场景层", abbr: "景",
    subs: [
      { id: "environment",        name: "环境描写" },
      { id: "character_portrayal",name: "人物刻画" },
      { id: "dialogue",           name: "对话写法" },
      { id: "sensory_priority",   name: "感官优先" },
    ],
  },
  {
    id: "theme", name: "主题层", abbr: "题",
    subs: [
      { id: "emotional_tone",      name: "情感基调" },
      { id: "values",              name: "价值取向" },
      { id: "motifs",              name: "母题意象" },
      { id: "narrative_philosophy",name: "叙事哲学" },
    ],
  },
];

/* 子维度路径 language.sentence_structure → { abbr, layer, name }；认不出时 name 就是路径本身 */
export function srDimMeta(path) {
  for (const l of SR_LAYERS) for (const s of l.subs) if (`${l.id}.${s.id}` === path) return { abbr: l.abbr, layer: l.name, name: s.name };
  return { abbr: "·", layer: "", name: path };
}

/* 段落类型（后端 8 类）的中文名：概览分布、示例预览、样例窗口共用 */
export const SR_PARA_LABEL = {
  narration: "叙述", dialogue: "对话", description_env: "环境", psychology: "心理",
  action: "动作", description_char: "人物", transition: "转场", flashback: "闪回",
};

/* 观察的置信度：矩阵格子、证据抽屉、画像维度摘要共用 */
export const SR_CONF_LABEL = { high: "高置信", medium: "中置信", low: "低置信", none: "暂无观察", skip: "语料不足" };
export const SR_CONF_TONE = { high: "ok", medium: "warn", low: "info" };

/* ---- 统计指标：总览、画像基线与回测共用同一组展示名与格式 ----
   SR_METRIC_META 是后端所有指标键的展示名（回测的量化对齐逐项列出）；
   SR_METRIC_DEFS 是总览 / 画像基线挑出来展示的 8 项，按这个顺序。 */
export const SR_METRIC_META = {
  avg_sentence_length: { name: "平均句长", unit: "字" },
  sentence_length_std: { name: "句长波动", unit: "字" },
  short_sentence_ratio: { name: "短句占比", pct: true },
  long_sentence_ratio: { name: "长句占比", pct: true },
  punctuation_density_per_1k: { name: "标点 / 千字", unit: "" },
  dash_em_density_per_1k: { name: "破折号 / 千字", unit: "" },
  ellipsis_density_per_1k: { name: "省略号 / 千字", unit: "" },
  semicolon_density_per_1k: { name: "分号 / 千字", unit: "" },
  question_density_per_1k: { name: "问号 / 千字", unit: "" },
  classical_word_ratio: { name: "文言用法", pct: true },
  colloquial_marker_ratio: { name: "口语标记占比", pct: true },
  metaphor_density_per_1k: { name: "比喻 / 千字", unit: "" },
  personification_density_per_1k: { name: "拟人 / 千字", unit: "" },
  dialogue_ratio: { name: "对话占比", pct: true },
  psychology_ratio: { name: "心理占比", pct: true },
  description_env_ratio: { name: "环境占比", pct: true },
  description_char_ratio: { name: "人物占比", pct: true },
  action_ratio: { name: "动作占比", pct: true },
  narration_ratio: { name: "叙述占比", pct: true },
  transition_ratio: { name: "转场占比", pct: true },
  flashback_ratio: { name: "闪回占比", pct: true },
  sensory_visual_per_1k: { name: "视觉描写 / 千字", unit: "" },
  sensory_auditory_per_1k: { name: "听觉描写 / 千字", unit: "" },
  sensory_olfactory_per_1k: { name: "嗅觉描写 / 千字", unit: "" },
  sensory_tactile_per_1k: { name: "触觉描写 / 千字", unit: "" },
  sensory_gustatory_per_1k: { name: "味觉描写 / 千字", unit: "" },
};

export const SR_METRIC_DEFS = [
  "avg_sentence_length", "sentence_length_std", "short_sentence_ratio", "dialogue_ratio",
  "metaphor_density_per_1k", "classical_word_ratio", "sensory_visual_per_1k", "dash_em_density_per_1k",
].map((key) => ({ key, ...SR_METRIC_META[key] }));

/* 认不出的指标键原样显示，不猜单位 */
export function srMetricMeta(key) {
  return SR_METRIC_META[key] || { name: key, unit: "" };
}

/* 百分数：小于 10% 保留一位小数（0.4% 不该写成 0%），否则取整。 */
export function srFormatPct(value) {
  const n = Number(value) * 100;
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  const text = abs > 0 && abs < 10 ? n.toFixed(1).replace(/\.0$/, "") : String(Math.round(n));
  return `${text}%`;
}

function srFormatNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return String(Math.round(n * 10) / 10);
}

/* 一项指标 → { key, name, value, unit, spread }；百分比指标的波动与均值同样换算成百分点。 */
export function srFormatMetric(def, stat) {
  if (!def || !stat || stat.mean == null || !Number.isFinite(Number(stat.mean))) return null;
  const fmt = def.pct ? srFormatPct : srFormatNumber;
  const std = Number(stat.std);
  return {
    key: def.key,
    name: def.name,
    value: fmt(stat.mean),
    unit: def.pct ? "" : (def.unit || ""),
    spread: Number.isFinite(std) ? `±${fmt(std)}` : null,
  };
}

/* stats_json.metrics / profile_json.metrics_baseline → 按 SR_METRIC_DEFS 顺序的可显示行（缺项跳过）。 */
export function srMetricRows(source) {
  const m = source && typeof source === "object" ? source : {};
  return SR_METRIC_DEFS.map((def) => srFormatMetric(def, m[def.key])).filter(Boolean);
}

/* 段落 id 形如 sr_para_<书>_<序号>（序号从 0 起）→「第 N 段」；认不出就返回 null，由调用方退回「引文」。 */
export function srParagraphLabel(paragraphId) {
  const m = /_(\d+)$/.exec(String(paragraphId || ""));
  if (!m) return null;
  return `第 ${(Number(m[1]) + 1).toLocaleString()} 段`;
}

/* ---- 注入策略：界面上的名称（A / B / C / A+B 是简写，留作徽标）---- */
export const SR_STRATEGIES = [
  { id: "mixed", code: "A+B", name: "规则 + 样例", desc: "规则写进提示，同时附上原书样例窗口；推荐", recommended: true },
  { id: "A", code: "A", name: "只用规则", desc: "把观察与禁忌写成规则放进提示，不带原文样例" },
  { id: "B", code: "B", name: "只用样例", desc: "整段摘取原书里的连续段落做示范" },
  { id: "C", code: "C", name: "相近片段", desc: "按场景检索相近的句段；比样例窗口弱，留作对照" },
];

export function srStrategyCode(strategy) {
  const hit = SR_STRATEGIES.find((s) => s.id === strategy);
  return hit ? hit.code : String(strategy || "—");
}

export function srStrategyName(strategy) {
  const hit = SR_STRATEGIES.find((s) => s.id === strategy);
  return hit ? hit.name : String(strategy || "—");
}

/* ---- 起草方式（2026-09-12 风格直起，Step 2）：落 binding.config_json.draft_mode ----
   style_first（缺省）= 首稿直接以参考作者手笔写；neutral_first = 现状流程（中性稿再上风格），作阅读对照。
   缺省不落库时后端按 injection_budget.yaml 的 draft_mode_default（style_first）生效。 */
export const SR_DRAFT_MODE_DEFAULT = "style_first";
export const SR_DRAFT_MODES = [
  { id: "style_first", label: "作者手笔直起", badge: "默认", detail: "首稿直接以参考作者手笔写；系统的房风质量门让位" },
  { id: "neutral_first", label: "中性稿再上风格", badge: "", detail: "现状流程，用于阅读对照" },
];

/* 绑定行 / 叠层行的起草方式标签：config_json.draft_mode 缺省即后端默认 style_first */
export function srDraftModeLabel(mode) {
  const hit = SR_DRAFT_MODES.find((m) => m.id === mode);
  return (hit || SR_DRAFT_MODES[0]).label;
}

/* ---- 绑定作用域：注入应用的表单、当前应用列表与叠加层共用 ---- */
export const SR_SCOPE_NAME = { project: "项目", scene: "场景", character: "角色", global: "全部作品" };
export const SR_SCOPE_TONE = { scene: "ok", character: "warn", project: "accent", global: "neutral" };
export const SR_SCOPE_LABEL = { scene: "场景层", character: "角色层", project: "项目层", global: "全局底层" };

/* 绑定是否生效（后端缺 status 视为 active） */
export function srBindingActive(binding) {
  return !!binding && (binding.status == null || binding.status === "active");
}

/* 同作用域遮蔽判定：所选 scope + scope_ref_id 上已有 active 绑定则返回该绑定 */
export function findShadowedBinding(bindings, scope, scopeRefId) {
  if (!Array.isArray(bindings) || !scope) return null;
  const ref = scopeRefId == null ? null : String(scopeRefId);
  return bindings.find((b) => b && b.scope === scope && srBindingActive(b)
    && (ref == null ? b.scope_ref_id == null : String(b.scope_ref_id) === ref)) || null;
}

/* 注入维度选项：按画像 profile_json.sub_dimensions（键为 16 个 sub_dim 路径）动态生成；
   画像缺失时回退 book.stats_json.input_assessment（layer 级 skip 才禁用整层）；
   两者都没有则全部可选。返回 { layers:[{id,name,abbr,subs:[{id,path,name,available,conf,obs,fp,q}]}], available:[path] } */
export function buildDimOptions(profile, book) {
  const subDims = (profile && profile.profile_json && profile.profile_json.sub_dimensions) || null;
  const hasSubDims = !!(subDims && typeof subDims === "object" && Object.keys(subDims).length > 0);
  const inputAssessment = (book && book.stats_json && book.stats_json.input_assessment) || null;
  const available = [];
  const layers = SR_LAYERS.map((l) => {
    const layerSkipped = !hasSubDims && !!(inputAssessment && inputAssessment[l.id] === "skip");
    const subs = l.subs.map((s) => {
      const path = `${l.id}.${s.id}`;
      const d = hasSubDims ? subDims[path] : null;
      const isAvailable = hasSubDims ? !!d : !layerSkipped;
      if (isAvailable) available.push(path);
      return {
        id: s.id, path, name: s.name, available: isAvailable,
        conf: (d && d.confidence) || null,
        obs: (d && d.observation_count) || 0,
        fp: (d && d.forbidden_pattern_count) || 0,
        q: (d && d.quote_count) || 0,
      };
    });
    return { id: l.id, name: l.name, abbr: l.abbr, subs, skipped: layerSkipped };
  });
  return { layers, available, source: hasSubDims ? "profile" : inputAssessment ? "input_assessment" : "none" };
}

/* 强度读数：只消费注入预览端点返回的 stats，缺失返回 null（调用方显示「预览中…」），
   绝不用本地公式虚构。stats 形状：{positive_lines, forbidden_lines, metric_lines, voice_lines,
   few_shot_windows, few_shot_chars, rag_snippets, total_prefix_chars, ...} */
export function computeIntensityReadout(stats) {
  if (!stats || typeof stats !== "object") return null;
  const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
  const ruleLines = num(stats.positive_lines) + num(stats.forbidden_lines) + num(stats.metric_lines);
  const voiceLines = num(stats.voice_lines);
  const sampleWindows = num(stats.few_shot_windows) + num(stats.rag_snippets);
  const totalChars = num(stats.total_prefix_chars);
  return {
    ruleLines, voiceLines, sampleWindows, totalChars,
    text: `规则 ${ruleLines} 行 · 声音特征 ${voiceLines} 行 · 样例 ${sampleWindows} 段 · 共 ${totalChars} 字`,
  };
}

/* 再合成判定：没有画像 → 可合成；有画像但 (a) 最新完成 run 与画像 run_id 不同、
   (b) coverage_json.stale、(c) status !== "active" → 允许重新合成；否则只能查看。 */
export function computeResynthState(deep) {
  const profile = deep && deep.profile;
  const runId = deep && deep.runId;
  if (!profile) return { hasProfile: false, canResynth: true, reason: "no_profile" };
  const cov = profile.coverage_json || {};
  if (runId && profile.run_id && runId !== profile.run_id) return { hasProfile: true, canResynth: true, reason: "new_run" };
  if (cov.stale === true) return { hasProfile: true, canResynth: true, reason: "stale" };
  if (profile.status !== "active") return { hasProfile: true, canResynth: true, reason: "inactive" };
  return { hasProfile: true, canResynth: false, reason: null };
}

export const SR_RESYNTH_REASON_LABEL = {
  new_run: "有新的抽取结果",
  stale: "画像已失效",
  inactive: "画像未激活",
};

/* 合成失败的作者可读文案：模型未接入 / 云端策略阻断 / 已在合成 /
   STYLE_REFERENCE_SYNTHESIZE_FAILED(409, details.reason_code) */
export function srSynthErrorMessage(e) {
  const code = (e && e.code) || "";
  if (code === "STYLE_REFERENCE_LLM_REQUIRED" || code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED") {
    return "合成风格画像需要先接入模型（设置 → 模型与接入）。";
  }
  if (code === "STYLE_REFERENCE_SYNTHESIS_ALREADY_ACTIVE") {
    return `这本书的画像正在合成，等它完成后再试（${SR_ACTIVITY_WHERE}里可看进度）。`;
  }
  if (code === "STYLE_REFERENCE_SYNTHESIZE_FAILED") {
    const reason = (e && e.details && e.details.reason_code) || "";
    const map = {
      budget_unfit: "观察太多，装不进合成预算：回维度矩阵驳回一部分后重试。",
      empty_profile: "没有可用的观察（全部被驳回或语料不足）：先重跑抽取。",
      source_overlap: "合成结果与原文重合太多，已被拦下：请重试合成。",
      text_integrity: "合成结果没通过文本完整性检查：请重试合成。",
      llm_failed: "模型调用失败：检查模型接入后重试。",
    };
    return "合成失败：" + (map[reason] || (e && e.message) || reason || "原因不明");
  }
  return "合成失败：" + ((e && e.message) || e);
}

/* 最新完成的 run：按 finished_at / started_at 降序取第一条 done；列表无时间戳时取末尾（插入序）。
   没有 done 时退到最新一条（running / failed），供概览显示进展。 */
export function srPickLatestRun(runs) {
  const list = Array.isArray(runs) ? runs.filter(Boolean) : [];
  if (!list.length) return null;
  const ts = (r) => String(r.finished_at || r.started_at || "");
  const done = list.filter((r) => r.status === "done");
  if (done.length) {
    const stamped = done.filter((r) => ts(r));
    if (stamped.length === done.length) return [...done].sort((a, b) => (ts(a) < ts(b) ? 1 : ts(a) > ts(b) ? -1 : 0))[0];
    return done[done.length - 1];
  }
  return list[list.length - 1];
}

/* 旧的书库徽标键 / 文案（后端 book.status：ready / ingesting / cancelling / failed）。
   界面现在一律用 srBookPipeline 的说法；这两个函数只留给导出契约的老调用方。 */
export function srMapStatus(s) {
  if (s === "ready") return "ready";
  if (s === "ingesting") return "importing";
  if (s === "cancelling") return "cancelling";
  if (s === "failed") return "failed";
  if (/extract|run/i.test(s || "")) return "extracting";
  return "pending";
}

export function srRunLabel(status) {
  if (status === "ready") return "已导入";
  if (status === "ingesting") return "段落分类中";
  if (status === "cancelling") return "正在取消分类";
  if (status === "failed") return "分类未完成";
  return status;
}

/* 「参考书活动」面板在哪：宽屏在左栏书库里，≤1280 在页头「参考书库」打开的书库里——
   两种宽度下这句话都对，提示与说明文字一律这样说。 */
export const SR_ACTIVITY_WHERE = "「参考书库」的「参考书活动」";

/* ISO 时间 → 「9 月 20 日 14:05」（回测页的「上次回测」）；认不出返回 null */
export function srFormatWhen(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return null;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${hh}:${mm}`;
}

/* 秒 → m:ss（活动面板与回测进度共用） */
export function srFormatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* ---- 每本书的流水线状态（书库条目、页头徽标共用）----
   ctx: {
     profiles: 该书的画像行 | null（未知），
     applied: 是否用于当前作品 | null（未知），
     deep: 已加载的深层数据 | null，
     running: { classify, extract, synthesize } 各为 { percentText } | null
   }
   返回 { key, label, tone }，tone 取 ws-ui 的 accent|ok|warn|danger|info|neutral。 */
export function srChooseProfile(profiles) {
  const list = Array.isArray(profiles) ? profiles.filter(Boolean) : [];
  if (!list.length) return null;
  return list.find((p) => p.status === "active") || list[list.length - 1];
}

function srProfileStale(profile) {
  return !!(profile && profile.coverage_json && profile.coverage_json.stale);
}

export function srBookPipeline(book, ctx = {}) {
  const running = ctx.running || {};
  const raw = book && book.rawStatus;
  if (running.classify) return { key: "classifying", label: `分类中 ${running.classify.percentText || ""}`.trim(), tone: "warn" };
  if (raw === "cancelling") return { key: "cancelling", label: "取消中", tone: "neutral" };
  if (raw === "ingesting") return { key: "classifying", label: "分类中", tone: "warn" };
  if (raw === "failed") return { key: "classify_failed", label: "分类未完成", tone: "danger" };
  if (running.extract) return { key: "extracting", label: `抽取中 ${running.extract.percentText || ""}`.trim(), tone: "warn" };
  if (running.synthesize) return { key: "synthesizing", label: "合成画像中", tone: "warn" };

  const deep = ctx.deep && ctx.deep.loaded ? ctx.deep : null;
  const profile = deep ? deep.profile : srChooseProfile(ctx.profiles);
  const stale = srProfileStale(profile);
  if (ctx.applied) {
    return stale
      ? { key: "applied_stale", label: "当前作品在用 · 画像已失效", tone: "warn" }
      : { key: "applied", label: "当前作品在用", tone: "ok" };
  }
  if (profile) {
    if (stale) return { key: "profile_stale", label: "画像需重新合成", tone: "warn" };
    if (profile.status === "active") return { key: "profile_active", label: "画像已启用", tone: "info" };
    return { key: "profile_draft", label: "画像待应用", tone: "info" };
  }
  if (deep) {
    const runStatus = deep.run ? deep.run.status : null;
    if (runStatus === "done") return { key: "ready_to_synthesize", label: "待合成画像", tone: "info" };
    if (runStatus === "running" || runStatus === "queued") return { key: "extracting", label: "抽取中", tone: "warn" };
    if (runStatus === "failed" || runStatus === "cancelled") return { key: "extract_failed", label: "抽取未完成", tone: "danger" };
    return { key: "ready_to_extract", label: "待抽取", tone: "neutral" };
  }
  if (Array.isArray(ctx.profiles)) return { key: "no_profile", label: "尚无画像", tone: "neutral" };
  return { key: "imported", label: "已导入", tone: "neutral" };
}

/* ---- 流水线五步的状态（步骤条）----
   每步 done | running | attention | todo | blocked，全部来自真实数据，不再按作者点到哪步打勾。
   回测是可选的一步：没有报告只算 todo，不挡后面的应用。
   这本书的深层数据还没读到时，后四步是 unknown（不画状态、不念状态）：没读到不等于「未开始」，
   否则一本全做完的书会先闪一下「维度矩阵 未开始 / 等前一步」，旁边页头却写着「当前作品在用」。 */
export const SR_STAGE_STATE_LABEL = {
  done: "已完成", running: "进行中", attention: "需处理", todo: "未开始", blocked: "等前一步", unknown: "",
};

export function srStageStates(book, deep, running = {}) {
  const raw = book && book.rawStatus;
  const states = {};
  if (running.classify || raw === "ingesting" || raw === "cancelling") states.overview = "running";
  else if (raw === "failed") states.overview = "attention";
  else states.overview = "done";

  const d = deep && deep.loaded ? deep : null;
  if (!d) {
    // 分类没完成时矩阵确实在等前一步（这只看书本身的状态）；其余要等深层数据到了再说
    states.matrix = states.overview !== "done" ? "blocked" : running.extract ? "running" : "unknown";
    states.profile = running.synthesize ? "running" : "unknown";
    states.validation = "unknown";
    states.apply = "unknown";
    return states;
  }
  const profile = d.profile;
  const runStatus = d.run ? d.run.status : null;

  if (states.overview !== "done") states.matrix = "blocked";
  else if (running.extract || runStatus === "running" || runStatus === "queued") states.matrix = "running";
  else if (runStatus === "done") states.matrix = "done";
  else if (runStatus === "failed" || runStatus === "cancelled") states.matrix = "attention";
  else states.matrix = "todo";

  if (running.synthesize) states.profile = "running";
  else if (profile) {
    const newRun = !!(d.runId && profile.run_id && d.runId !== profile.run_id);
    states.profile = srProfileStale(profile) || newRun ? "attention" : "done";
  } else states.profile = states.matrix === "done" ? "todo" : "blocked";

  const hasProfile = !!profile;
  const reports = Array.isArray(d.reports) ? d.reports : [];
  if (!hasProfile) states.validation = "blocked";
  else if (reports.some((r) => r && (r.status === "running" || r.status === "pending"))) states.validation = "running";
  else states.validation = reports.some((r) => r && r.verdict) ? "done" : "todo";

  const bindings = Array.isArray(d.bindings) ? d.bindings : [];
  if (!hasProfile) states.apply = "blocked";
  else states.apply = bindings.some(srBindingActive) ? "done" : "todo";
  return states;
}

/* 落点：已用于当前作品的书直接落在「注入应用」；否则落在第一个没做完的必经步骤
   （回测可选，不作为落点）；全做完也落在「注入应用」。 */
export function srLandingStage(states, { applied = false } = {}) {
  if (applied) return "apply";
  for (const id of ["overview", "matrix", "profile", "apply"]) {
    if (states && states[id] !== "done") return id;
  }
  return "apply";
}

/* ---- 书库排序与筛选 ---- */
export function srSortBooks(books, appliedBookIds) {
  const list = Array.isArray(books) ? books.slice() : [];
  if (!appliedBookIds || !appliedBookIds.size) return list;
  return list
    .map((b, i) => ({ b, i, pin: appliedBookIds.has(b.id) ? 0 : 1 }))
    .sort((x, y) => (x.pin - y.pin) || (x.i - y.i))
    .map((x) => x.b);
}

export function srFilterBooks(books, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return books;
  return books.filter((b) => `${b.title || ""} ${b.author || ""}`.toLowerCase().includes(q));
}

/* 书脊颜色按书 id 固定（以前按列表位置轮换，置顶一本后所有书都换色）。 */
const SR_SPINE_COLORS = ["crimson", "gold", "slate", "sage"];
export function srSpineColor(bookId) {
  let h = 0;
  for (const ch of String(bookId || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return SR_SPINE_COLORS[h % SR_SPINE_COLORS.length];
}

/* ---- 界面偏好 ws_sr_ui_v1（每位读者本机：上次打开的书与步骤）----
   形状：{ last: { bookId, stage }, works: { [workId]: { bookId, stage } } }。
   只是便利：读不到、写不进（隐私窗口、清过站点数据）都当没有。 */
export const SR_UI_PREFS_KEY = "ws_sr_ui_v1";

export function srReadUiPrefs(storage) {
  try {
    const store = storage || globalThis.localStorage;
    const raw = store && store.getItem(SR_UI_PREFS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (!parsed || typeof parsed !== "object") return { last: null, works: {} };
    return {
      last: parsed.last && typeof parsed.last === "object" ? parsed.last : null,
      works: parsed.works && typeof parsed.works === "object" ? parsed.works : {},
    };
  } catch (e) {
    return { last: null, works: {} };
  }
}

export function srRememberUi(workId, entry, storage) {
  if (!entry || !entry.bookId) return;
  try {
    const store = storage || globalThis.localStorage;
    if (!store) return;
    const prefs = srReadUiPrefs(store);
    const value = { bookId: String(entry.bookId), stage: entry.stage || null };
    const works = { ...prefs.works };
    if (workId) works[workId] = value;
    // 只留最近 20 部作品的记录，别让偏好无限长
    const keys = Object.keys(works);
    if (keys.length > 20) keys.slice(0, keys.length - 20).forEach((k) => { delete works[k]; });
    store.setItem(SR_UI_PREFS_KEY, JSON.stringify({ last: value, works }));
  } catch (e) { /* 存不进就算了：偏好只是便利 */ }
}

/* 进入页面时打开哪本书：
   本次打开应用期间刚看过的那本（session：离开页面再回来接着看，不跨刷新）
   → 当前作品在用的书 → 这部作品上次打开的书 → 上次打开的书 → 第一本。
   本机记录（prefs）只在没有「在用」的书时才用：作者瞄过一眼别的书，下次进来仍回到给作品定调的那本。
   还不知道「当前作品在用哪本」（metaSettled=false）又没有 session 时返回 null，等一等再定，不先落到别处再跳。 */
export function srPickLandingBook(books, { prefs, workId, appliedBookIds, metaSettled = true, session = null } = {}) {
  const list = Array.isArray(books) ? books : [];
  if (!list.length) return null;
  const has = (id) => !!id && list.some((b) => b.id === id);
  if (session && has(session.bookId)) return { bookId: session.bookId, stage: session.stage || null, source: "session" };
  if (!metaSettled) return null;
  const applied = appliedBookIds ? list.find((b) => appliedBookIds.has(b.id)) : null;
  if (applied) return { bookId: applied.id, stage: null, source: "applied" };
  const p = prefs || { last: null, works: {} };
  const own = workId && p.works ? p.works[workId] : null;
  if (own && has(own.bookId)) return { bookId: own.bookId, stage: own.stage || null, source: "work" };
  if (p.last && has(p.last.bookId)) return { bookId: p.last.bookId, stage: p.last.stage || null, source: "last" };
  return { bookId: list[0].id, stage: null, source: "first" };
}
