/* ==========================================================
   雪花十步 · 纯推导（叶子模块：只 import 步骤文案 ws-snow-guide.js）
   ----------------------------------------------------------
   视图与右栏共用的确定性推导：折叠文本、09 的节奏 / 织线 / 结构核对与灾难推断、10 的逐场覆盖、
   分形管线、失效图与上游漂移、落点，以及本步要点与 AI 入口的小推导。
   从 ws-snow-model.js 拆出（2026-09-29）；ws-snow-model.js 原样转出这里的全部名字。
   ========================================================== */

import { S2_BE_KEY, S2_STEPS, s2FindStepKey } from "./ws-snow-guide.js";

// 折叠草稿 / 脚手架为一段纯文本（导出、引用上下文、回滚预览用）
export function s2Content(draft, scaffold) {
  let text = (draft || "").trim();
  if (!text && scaffold) {
    const out = [];
    const walk = (v) => {
      if (typeof v === "string") out.push(v);
      else if (Array.isArray(v)) v.forEach(walk);
      else if (v && typeof v === "object") Object.values(v).forEach(walk);
    };
    walk(scaffold);
    text = out.join("\n");
  }
  return text;
}

/* ---- 09 场景列表：节奏、织线与结构核对 ---- */

// 连续同类型的“跑动”：主动跑很长 = 提醒作者想想要不要喘息（≥5 才提）；反应跑太长 = 松散（≥3 就提）
// 阶段 B：Ingermanson 说反应场是少数，一场挫折后可以直接开下一场主动——连续主动本身不是问题。
export function s2PacingRuns(list) {
  const runs = [];
  (list || []).forEach((s, i) => {
    const t = s.type === "proactive" ? "pro" : "rea";
    const last = runs[runs.length - 1];
    if (last && last.t === t) { last.len++; last.end = i; }
    else runs.push({ t, len: 1, start: i, end: i });
  });
  const tight = runs.filter(r => r.t === "pro" && r.len >= 5);
  const slack = runs.filter(r => r.t === "rea" && r.len >= 3);
  return { runs, tight, slack };
}

// 每条线在全书的分布：出现位置、跨度、是否扎堆、是否缺“折射道德前提”
export function s2LineStats(list, lines) {
  const n = (list || []).length || 1;
  return (lines || []).map(ln => {
    const pos = [];
    (list || []).forEach((s, i) => { if ((s.line || "main") === ln.id) pos.push(i); });
    const count = pos.length;
    const span = count ? (pos[count - 1] - pos[0] + 1) : 0;
    const clustered = count >= 2 && span / n < 0.34;            // 像“绕路”而非“编织”
    const noRefract = ln.kind !== "main" && !(ln.refract || "").trim();
    return { ...ln, pos, count, span, clustered, noRefract };
  });
}

/* 还没有支线时，09 只有这一条主线（表格、织线、结构核对共用同一个对象，memo 才认得出「没变」） */
export const S2_DEFAULT_LINES = Object.freeze([Object.freeze({ id: "main", name: "主线", kind: "main", tone: "crimson", refract: "" })]);
export function s2SceneLines(scaffold) {
  const lines = scaffold && scaffold.lines;
  return lines && lines.length ? lines : S2_DEFAULT_LINES;
}

/* 09 场景表的统计：表头计数、织线、节奏提示与右栏的结构核对共用一份。
   按 (list, lines) 的对象身份记住上一次的结果——键入时表格与右栏在同一次渲染里各要一次，
   以前两边各算一遍（节奏、织线、每场两次灾难推断）。 */
const sceneStatsCache = new WeakMap();
export function s2SceneListStats(list, lines) {
  const rows = Array.isArray(list) ? list : [];
  const lns = lines || S2_DEFAULT_LINES;
  const hit = sceneStatsCache.get(rows);
  if (hit && hit.lines === lns) return hit.stats;
  const pacing = s2PacingRuns(rows);
  const inferred = rows.map(s => (s.spine ? "" : s2InferSpine(s.fn)));
  const pro = rows.filter(s => s.type === "proactive").length;
  const stats = {
    pacing,
    lineStats: s2LineStats(rows, lns),
    inferred,
    pro,
    rea: rows.length - pro,
    noCrucible: rows.filter(s => !(s.crucible || "").trim()).length,
    spineHit: rows.filter((s, i) => s.spine || inferred[i]).length,
    inferredHit: inferred.filter(Boolean).length,
    tightMax: pacing.tight.length ? Math.max(...pacing.tight.map(r => r.len)) : 0,
    slackMax: pacing.slack.length ? Math.max(...pacing.slack.map(r => r.len)) : 0,
  };
  sceneStatsCache.set(rows, { lines: lns, stats });
  return stats;
}

// 09 场景列表的结构核对：只读织线 / 节奏的确定性结构，不打分（与场景表的统计同一份）。
export function s2SceneAuto(scaffold) {
  const list = (scaffold && scaffold.list) || [];
  const { noCrucible: noCru, tightMax, lineStats } = s2SceneListStats(list, s2SceneLines(scaffold));
  const clustered = lineStats.filter(s => s.clustered).length;
  return [
    { t: "场场有冲突", pass: list.length > 0 && noCru === 0, val: !list.length ? "还没有场景" : noCru ? `${noCru} 场还没写坩埚` : `${list.length} 场都写了坩埚` },
    // 阶段 B：反应场是少数（Ingermanson），连续主动只是提醒——阈值放宽到 5，且不算硬标准
    { t: "节奏（建议）", pass: tightMax < 5, val: tightMax >= 5 ? `连续 ${tightMax} 场主动，中间没有喘息` : "没有过长的连续主动" },
    { t: "支线不扎堆", pass: clustered === 0, val: clustered ? `${clustered} 条线挤在一小段里` : "各条线分布均匀" },
  ];
}

/* 功能标签里的灾难标记（只读推断，不写回）：与后端 spine_from_role 同一族写法——
   灾一 / 灾难一 / 灾难 1 / 第一个灾难 / Disaster 1。09 没显式标脊柱时，分章面板按它认灾难，
   场景表也按它显示，两边对「灾难落在哪一场」不再各说各话。 */
const S2_SPINE_ROLE_RE = /灾难?\s*([一二三123])|第\s*([一二三123])\s*(?:个|次|场|重)?\s*灾难?|disaster\s*#?\s*([123])/i;
const S2_SPINE_BY_ORDINAL = { "一": "灾一", "1": "灾一", "二": "灾二", "2": "灾二", "三": "灾三", "3": "灾三" };
export function s2InferSpine(fn) {
  const m = S2_SPINE_ROLE_RE.exec(String(fn || ""));
  if (!m) return "";
  return S2_SPINE_BY_ORDINAL[m[1] || m[2] || m[3] || ""] || "";
}
/* 灾难标记的下拉选项（07 章表与 09 场景表共用） */
export const S2_SPINE_OPTS = ["", "灾一", "灾二", "灾三"];

// 阶段 M：拖拽换位——把 from 位置的场挪到 to 位置（其余顺序不变），纯函数，供单测
export function s2ReorderScenes(list, from, to) {
  const items = Array.isArray(list) ? list.slice() : [];
  if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) return items;
  const [moved] = items.splice(from, 1);
  items.splice(to, 0, moved);
  return items;
}

/* 09 场景行的 id 就是上行到后端的 row_uid —— 场景计划的不可变身份锚，必须全局不重号。
   旧写法 `"S" + (list.length + 1)` 只看当前长度：删掉中间一场再新增，铸出的号会撞上
   仍然存活的那一场，后端按 row_uid 对位时后者整段覆盖前者的内容（构思侧丢戏）。
   规则改成「已用过的最大编号 + 1」，并兜底跳过任何仍被占用的号。 */
export function s2NextSceneRowId(list) {
  const rows = Array.isArray(list) ? list : [];
  const used = new Set(rows.map(row => String((row && row.id) || "")));
  let next = rows.reduce((max, row) => {
    const n = parseInt(String((row && row.id) || "").replace(/^S/, ""), 10);
    return Number.isFinite(n) && n > max ? n : max;
  }, 0) + 1;
  while (used.has("S" + String(next).padStart(2, "0"))) next++;
  return "S" + String(next).padStart(2, "0");
}

/* 场景显示号：s.id 是不可变身份（真实项目里是 row_<uuid>，不宜直接示人）。
   已是 Sxx / 纯数字则规范化，否则按位置给个友好的 S01 号。 */
export function s2SceneNo(id, idx) {
  const s = String(id || "").trim();
  if (/^S\d{1,3}$/i.test(s)) return "S" + s.slice(1).padStart(2, "0");
  if (/^\d{1,3}$/.test(s)) return "S" + s.padStart(2, "0");
  return "S" + String((idx || 0) + 1).padStart(2, "0");
}

/* POV 显示/选择：场景的 pov 可能存的是角色 id（真实项目水合自 pov_character_id，
   形如 <project>_CHAR01）或姓名（手填）。名册（04 步）以 character_id 为键、
   值含 name。统一解析成显示姓名；下拉选择存回角色 id，与后端 pov_character_id 对齐。 */
export function s2RosterList(refs) {
  const chars = ((refs && refs.characters) || {}).chars || {};
  return Object.entries(chars).map(([id, c]) => ({ id, name: ((c && c.name) || "").trim() || id }));
}
export function s2PovLabel(pov, roster) {
  if (!pov) return "";
  const hit = (roster || []).find(r => r.id === pov);
  return hit ? hit.name : pov;  // 已是姓名 / 自由文本 → 原样
}

/* ---- 10 场景规划：逐场覆盖与链条核验 ---- */
/* 阶段 E：形态以 09 场景列表为真相——传入 type 时按它取三槽；只有拿不到 09 信息时才看存储的 plan.mode。
   以前覆盖格按存储的 mode 数槽，09 切换类型后格子说「三槽齐」、编辑器却是另一组空槽。 */
export function s2PlanSlots(plan, type) {
  const mode = type ? (type === "reactive" ? "reactive" : "proactive") : (plan && plan.mode);
  return mode === "reactive" ? ["reaction", "dilemma", "decision"] : ["goal", "conflict", "setback"];
}
// 0 = 未规划 · 1 = 填了一半 · 2 = 三槽齐
export function s2PlanState(plan, type) {
  if (!plan) return 0;
  // 阶段 R：写了破例理由 = 这一场故意不按三拍走（原著：不过关也可放行，但要知道理由）——按已规划计
  if ((plan.exception || "").trim()) return 2;
  const slots = s2PlanSlots(plan, type);
  const n = slots.filter(f => (plan[f] || "").trim()).length;
  return n === slots.length ? 2 : n ? 1 : 0;
}
export function s2PlanAuto(scaffold, scenesScaffold) {
  const list = (scenesScaffold && scenesScaffold.list) || [];
  const plans = (scaffold && scaffold.plans) || {};
  const total = list.length;
  const stateOf = (s) => s2PlanState(plans[s.id], s.type);
  const fully = list.filter(s => stateOf(s) === 2).length;
  const partial = list.filter(s => stateOf(s) === 1).length;
  let seamBad = 0; // 已规划的场，它的上一场却还空着 → 「挫败→反应 / 决定→目标」的链条断在那里
  list.forEach((s, i) => { if (i > 0 && stateOf(s) > 0 && stateOf(list[i - 1]) === 0) seamBad++; });
  return [
    { t: "逐场覆盖", pass: total > 0 && fully + partial === total, val: total ? `${fully + partial}/${total} 场已规划` : "09 还没有场景", need: "每场一份" },
    { t: "三槽填满", pass: total > 0 && fully === total, val: partial ? `${partial} 场只填了一半` : `${fully}/${total} 场三槽齐` , need: "每场三拍都填上" },
    { t: "链条衔接", pass: seamBad === 0, val: seamBad ? `${seamBad} 处断链` : "挫败→反应 顺接", need: "上一场也已规划" },
  ];
}

// 阶段 R：cut（待删）是作者专用的裁定——原著「杀要杀得对：不真删，标记待删，下一稿再删」；该重写 / 待删的场不物化、不阻断全书
export const S2_TRIAGE_LABEL = { pass: "可通过", maybe: "需修补", rewrite: "该重写", cut: "待删" };
export const S2_VERDICTS = ["pass", "maybe", "rewrite", "cut"];

// 分形管线：本步在雪花展开链上的位置（上游 → 本步×倍率 → 下游）
export function s2Pipeline(stepKey) {
  const step = S2_STEPS.find(s => s.key === stepKey);
  if (!step) return null;
  const downs = S2_STEPS.filter(s => s.fromKey === stepKey);
  return {
    inName: step.from || "雪花原点", inKey: step.fromKey || null,
    ratio: step.grow,
    outName: downs.length ? downs.map(d => d.name).join(" / ") : "正文初稿",
    outKey: downs.length ? downs[0].key : null,
  };
}

/* ---- downstream staleness (the fractal method's cheap-backtracking core) ----
   阶段 E（E3 第二步）：失效的单一真相在后端。后端在再次批准上游时按「本步消费的字段」的
   签名判定失效（status=stale + stale_reason），前端只读它——本地的 revs / confirmRevs 图
   已移除，不再有第二套失效算法。另外按本步 artifact.input_refs（写入时消费的上游
   step_run_id）对照各上游现在的 step_run_id，得到「哪些上游已有新版本」：这是给作者的
   方向指引与上游 diff 的依据，不是失效判定——后端没标 stale 的步骤不显示「需复核」。 */
/* 依赖是 DAG 而非单亲链：fromKey 是主展开源，alsoFrom 是跨轨依赖
   （如 05 梗概 / 09 场景列表也依赖 04 角色表：改角色同样触发复核）。引用面板用它列上游。 */
export function s2Ancestors(key) {
  const out = []; const seen = new Set([key]); let frontier = [key];
  while (frontier.length) {
    const next = [];
    frontier.forEach(k => {
      const s = S2_STEPS.find(x => x.key === k);
      const parents = s ? [s.fromKey, ...(s.alsoFrom || [])].filter(Boolean) : [];
      parents.forEach(p => { if (!seen.has(p)) { seen.add(p); out.push(p); next.push(p); } });
    });
    frontier = next;
  }
  return out;
}
// 本步写入时消费的上游版本（input_refs）与各上游现在的版本不同 → 这些上游「已有新版本」
export function s2UpstreamDrift(health, key) {
  const refs = (((health || {})[key]) || {}).inputRefs || {};
  return S2_STEPS.filter(s => {
    const oldRun = refs[S2_BE_KEY[s.key]];
    const now = (((health || {})[s.key]) || {}).stepRunId;
    return !!(oldRun && now && oldRun !== now);
  }).map(s => s.key);
}
// 需复核图：只有后端 status=stale 且作者尚未「确认仍有效」的步骤；值是漂移的上游列表（可能为空）
export function s2StaleMap(health) {
  const map = {};
  S2_STEPS.forEach(s => {
    const b = (health || {})[s.key];
    if (b && b.beStatus === "stale" && !b.staleAcceptedAt) map[s.key] = s2UpstreamDrift(health, s.key);
  });
  return map;
}

/* 打开构思时落在哪一步：先是后端判定需复核的第一步，再是还没确认（也没略过）的第一步，
   都没有就回到上次看的那一步（十步都确认过、也没有记录时停在最后一步）。
   以前每次进来都落在 03 一段话概括，不管哪一步需复核、哪一步还空着。 */
export function s2LandingStep({ states, health, lastVisited } = {}) {
  const stale = s2StaleMap(health);
  const firstStale = S2_STEPS.find(s => stale[s.key]);
  if (firstStale) return firstStale.key;
  const st = states || {};
  const unfinished = S2_STEPS.find(s => { const v = st[s.key] || "todo"; return v !== "done" && v !== "skip"; });
  if (unfinished) return unfinished.key;
  const last = s2FindStepKey(lastVisited);
  return last || S2_STEPS[S2_STEPS.length - 1].key;
}

/* 服务端以「前面的步骤还没确认」拒绝确认 / 复核 / 略过（SNOWFLAKE_PREVIOUS_STEP_REQUIRED）时卡在哪一步：
   错误详情 missing_previous_steps 按步序排，第一项就是该先去的那一步。返回 { key, label, more }——key 是前端步骤键
   （认不出的后端键给 null，只报它的名字），more 是后面还差几步；不是这种错误返回 null。 */
export function s2BlockedStep(err) {
  const missing = (err && err.details && Array.isArray(err.details.missing_previous_steps)) ? err.details.missing_previous_steps : [];
  const first = missing[0];
  if (!first) return null;
  const st = S2_STEPS.find(s => s.be === first.step_key) || null;
  return {
    key: st ? st.key : null,
    label: st ? `${st.num} ${st.name}` : String(first.label || first.step_key || "前面的步骤"),
    more: missing.length - 1,
  };
}

/* ---- 本步要点（阶段 T / U）与 AI 入口的小推导 ---- */
export const BRIEF_KIND_LABEL = { decision: "决定", rejection: "否决", constraint: "约束", pending: "待定" };
export const BRIEF_KIND_ORDER = ["decision", "constraint", "rejection", "pending"];
/* 教练某轮对要点的差异 → 「+2 / 改 1 / 撤 1」 */
export function s2BriefDeltaParts(delta) {
  const d = delta || {};
  const parts = [];
  if ((d.added || []).length) parts.push(`+${d.added.length}`);
  if ((d.updated || []).length) parts.push(`改 ${d.updated.length}`);
  if ((d.superseded || []).length) parts.push(`撤 ${d.superseded.length}`);
  return parts;
}
/* 「生成中…」只亮在被点的那个入口：ai.busyTarget 是本步正在生成的入口（kind + 可选的成员 id） */
export function s2BusyOn(ai, kind, id) {
  const t = ai && ai.busyTarget;
  if (!ai || !ai.structBusy || !t || t.kind !== kind) return false;
  return id == null || t.id == null || t.id === id;
}
/* 本步当前版本的出处（后端 health）：AI 按哪个方向 / 哪版要点生成的 */
export function s2Provenance(health) {
  const h = health || {};
  if (h.generationSource !== "llm") return null;
  const dir = h.direction || null;
  const used = h.directionBrief || null;
  const bits = [];
  if (dir && dir.kind === "candidate") bits.push(`按方向「${dir.label || "方向"}」生成`);
  else if (dir && dir.kind === "coach_reply") bits.push("按教练回复生成");
  else bits.push("AI 生成");
  if (used && used.used === false) bits.push("未带要点");
  else if (used && typeof used.revision === "number" && used.revision > 0) bits.push(`带第 ${used.revision} 版要点`);
  return bits.join(" · ");
}
