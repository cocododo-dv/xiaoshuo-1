/* ==========================================================
   风格参考 ·「配置对比」的说法与判断（纯函数，node 单测）
   ----------------------------------------------------------
   后端：services/style_reference/ratchet_job.py（GET /profiles/{id}/ratchet、…/preview、POST 开跑 / 取消、
   GET /ratchet-runs/{job}、PUT …/votes）。一次比较 = 参考书里留出几段原文，提炼出不带原文措辞的情节要点，两套起草配置
   各照着写一遍，量谁离原文更近；同时出一份盲读——每段三份文字（原文、A、B）顺序打乱。
   先读后看：揭晓之前后端不给答案、结论和逐段数字；揭晓之后盲读选择不能再改。
   ========================================================== */

export const RATCHET_LABELS = ["甲", "乙", "丙"];

export const RATCHET_PLACEMENTS = [
  { value: "user_tail", label: "放在要求后面" },
  { value: "system", label: "放在系统提示里" },
];

const SOURCE_LABEL = { original: "原文", A: "配置 A", B: "配置 B" };

export function srRatchetSourceLabel(source) {
  return SOURCE_LABEL[source] || "";
}

/* 表单：A 默认 = 现在的配置；B 默认也从现在的配置起步（作者改一两项）。aa = 「同一配置再写一份」（量噪声） */
export function srRatchetDefaultForm(current, { passages = 4 } = {}) {
  const base = {
    model: (current && current.model) || "",
    windows: current && Number.isFinite(Number(current.windows)) ? Number(current.windows) : 6,
    placement: (current && current.placement) || "user_tail",
  };
  return { a: { ...base }, b: { ...base }, aa: false, passages };
}

function armBody(arm) {
  const body = {};
  if (arm.model) body.model = String(arm.model);
  if (arm.windows !== "" && arm.windows != null && Number.isFinite(Number(arm.windows))) body.windows = Math.round(Number(arm.windows));
  if (arm.placement) body.placement = arm.placement;
  return body;
}

export function srRatchetBody(form) {
  const a = armBody(form.a);
  const b = form.aa ? { ...a, rep: 2 } : armBody(form.b);
  return { a, b, passages: Number(form.passages) || 4 };
}

/* 两套配置一样又没勾 A/A：比不出东西 */
export function srRatchetSameArms(form) {
  if (form.aa) return false;
  const a = armBody(form.a);
  const b = armBody(form.b);
  return a.model === b.model && a.windows === b.windows && a.placement === b.placement;
}

/* 表单里 B 和 A 不一样的几项（B 那一栏在这几项旁边标「改」）；A/A 时没有 */
export function srRatchetArmDiff(form) {
  if (form.aa) return [];
  const a = armBody(form.a);
  const b = armBody(form.b);
  return ["model", "windows", "placement"].filter((key) => a[key] !== b[key]);
}

const placementLabel = (value) => (RATCHET_PLACEMENTS.find((p) => p.value === value) || {}).label || value || "";

/* 一次比较的名字：只说 B 改了什么（「样例 12 → 8 窗」）；两边只差「第几份」= 量噪声 */
export function srRatchetRunTitle(arms) {
  const a = arms && arms.A;
  const b = arms && arms.B;
  if (!a || !b) return "配置对比";
  const diffs = [];
  if (a.model !== b.model) diffs.push(`模型 ${a.model} → ${b.model}`);
  if (Number(a.windows) !== Number(b.windows)) diffs.push(`样例 ${a.windows} → ${b.windows} 窗`);
  if (a.placement !== b.placement) diffs.push(`样例${placementLabel(a.placement)} → ${placementLabel(b.placement)}`);
  if (!diffs.length) return `量噪声 · ${a.model} · 样例 ${a.windows} 窗`;
  return diffs.join("；");
}

function wanParts(chars) {
  const n = Number(chars) || 0;
  if (n >= 10000) return { value: (n / 10000).toFixed(n >= 100000 ? 0 : 1), unit: "万字" };
  return { value: n.toLocaleString("zh-CN"), unit: "字" };
}

/* 预估：三块统计（调几次模型、多少字输入、几份上次写过） */
export function srRatchetPreviewStats(preview) {
  if (!preview) return [];
  const drafts = preview.draft_calls || {};
  const parts = [];
  if (preview.plot_calls) parts.push(`要点 ${preview.plot_calls}`);
  if (drafts.A) parts.push(`A ${drafts.A}`);
  if (drafts.B) parts.push(`B ${drafts.B}`);
  const cached = Math.max(0, (preview.passages || []).length * 2 - (Number(drafts.A) || 0) - (Number(drafts.B) || 0));
  const input = wanParts(preview.input_chars_estimate);
  return [
    { key: "calls", label: "调用模型", value: Number(preview.calls) || 0, unit: "次", hint: parts.join(" · ") || "全写过了" },
    { key: "input", label: "输入约", value: input.value, unit: input.unit },
    { key: "cached", label: "上次写过", value: cached, unit: "份", hint: cached ? "不再写" : undefined },
  ];
}

/* 历史清单里一次比较的状态徽标 */
export function srRatchetRunTag(entry) {
  if (!entry) return { tone: "neutral", label: "" };
  if (entry.status === "queued" || entry.status === "running") return { tone: "warn", label: "进行中" };
  if (entry.status === "cancelled") return { tone: "neutral", label: "已取消" };
  if (entry.status === "failed") return { tone: "danger", label: "没有完成" };
  if (!entry.revealed) {
    const r = entry.reading || {};
    return { tone: "info", label: r.total ? `待盲读 ${r.done || 0}/${r.total}` : "待盲读" };
  }
  if (entry.same_config) return { tone: "neutral", label: entry.noise != null ? `噪声 ${Number(entry.noise).toFixed(3)}` : "A/A" };
  if (entry.winner === "B") return { tone: "ok", label: "B 更像" };
  if (entry.winner === "A") return { tone: "warn", label: "A 更像" };
  return { tone: "neutral", label: "分不出" };
}

/* 揭晓后的数字结论 */
export function srRatchetVerdictView(summary) {
  if (!summary || !summary.verdict) return null;
  const v = summary.verdict;
  const eps = Number(v.epsilon || 0).toFixed(3);
  const gain = Math.abs(Number(v.mean_gain || 0)).toFixed(3);
  if (summary.same_config) {
    return {
      tone: "neutral",
      title: `噪声：同一配置写两份，平均差 ${Number(summary.noise || 0).toFixed(3)}`,
      text: "这是两份之间的随机差别。之后的比较，差距要大过它才算数（已记下，自动用上）。",
    };
  }
  const why = summary.epsilon_source ? `（门槛 ${eps}：${summary.epsilon_source}）` : `（门槛 ${eps}）`;
  if (v.winner === "B") {
    return { tone: "ok", title: "B 更像", text: `${v.passages} 段里每段都是 B 离原文更近，平均近 ${gain}${why}。` };
  }
  if (v.winner === "A") {
    return { tone: "warn", title: "A 更像", text: `${v.passages} 段里每段都是 A 离原文更近，平均近 ${gain}${why}。换成 B 会变差。` };
  }
  return {
    tone: "neutral",
    title: "分不出",
    text: `B 更近 ${v.wins_b} 段、A 更近 ${v.wins_a} 段，平均差 ${gain}${why}。要每段都更近、平均差过门槛才算赢。`,
  };
}

/* 揭晓后的数字，排成一排统计 */
export function srRatchetVerdictStats(summary) {
  if (!summary || !summary.verdict) return [];
  const v = summary.verdict;
  if (summary.same_config) {
    return [
      { key: "noise", label: "噪声（平均差）", value: Number(summary.noise || 0).toFixed(3) },
      { key: "passages", label: "比了", value: v.passages, unit: "段" },
    ];
  }
  const view = srRatchetVerdictView(summary);
  const gain = Number(v.mean_gain || 0);
  return [
    { key: "winner", label: "结论", value: view.title, tone: view.tone },
    { key: "wins", label: "B 更近", value: `${v.wins_b}/${v.passages}`, unit: "段" },
    { key: "gain", label: gain >= 0 ? "B 平均近" : "A 平均近", value: Math.abs(gain).toFixed(3) },
    { key: "epsilon", label: "门槛", value: Number(v.epsilon || 0).toFixed(3), hint: summary.epsilon_source || undefined },
  ];
}

/* 作者读出来的结果（后端在揭晓后给 prefer / original_best）。A/A 两边是同一配置，谁排前面没有意义：只说认没认出原文 */
export function srRatchetReadingView(reading, { sameConfig = false } = {}) {
  if (!reading || !reading.prefer || !reading.done) return null;
  if (sameConfig) return { lead: null, text: `你读的：原文被你认作最像 ${reading.original_best}/${reading.done} 段。` };
  const p = reading.prefer;
  const lead = p.B > p.A ? "B" : p.A > p.B ? "A" : null;
  const parts = [`B 排在 A 前面 ${p.B} 段`, `A 在前 ${p.A} 段`];
  if (p.tie) parts.push(`分不出 ${p.tie} 段`);
  return {
    lead,
    text: `你读的：${parts.join("、")}；原文被你认作最像 ${reading.original_best}/${reading.done} 段。`,
  };
}

/* 数字和读的是否一致（分不出 / 读出平手都不算矛盾） */
export function srRatchetAgreement(summary, reading) {
  const verdict = summary && summary.verdict;
  const view = srRatchetReadingView(reading);
  if (!verdict || !verdict.winner || !view || !view.lead || (summary && summary.same_config)) return null;
  return verdict.winner === view.lead
    ? { tone: "ok", text: "数字和你读出来的一致。" }
    : { tone: "warn", text: "数字和你读出来的相反：以你读的为准，数字量的只是句长、标点、对白这些测得出的习惯。" };
}

/* 一段的选择：选「最像」时如果它原本是「最不像」，把那边清掉（反之亦然）；再点一次 = 取消 */
export function srRatchetToggleVote(vote, kind, label) {
  const current = { best: (vote && vote.best) || null, worst: (vote && vote.worst) || null };
  const other = kind === "best" ? "worst" : "best";
  if (current[kind] === label) return { ...current, [kind]: null };
  const next = { ...current, [kind]: label };
  if (next[other] === label) next[other] = null;
  return next;
}

export function srRatchetVoteDone(vote) {
  return !!(vote && vote.best && vote.worst);
}

export function srRatchetVotesDone(reading, votes) {
  const list = Array.isArray(reading) ? reading : [];
  return list.filter((item) => srRatchetVoteDone(votes && votes[String(item.index)])).length;
}

export function srRatchetUsageText(usage) {
  if (!usage || !usage.calls) return "这次没有调用模型（全用的上次写过的）。";
  const fmt = (n) => Number(n || 0).toLocaleString("zh-CN");
  return `调用模型 ${usage.calls} 次：输入 ${fmt(usage.prompt_tokens)} token，输出 ${fmt(usage.completion_tokens)} token。`;
}
