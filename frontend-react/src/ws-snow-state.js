/* ==========================================================
   雪花十步 · 空白脚手架与缓存归一（叶子模块：只 import 同层的步骤文案 ws-snow-guide.js 与纯推导 ws-snow-derive.js）
   ----------------------------------------------------------
   新作品的空白十步、本机缓存读入时的归一（缺的键补齐、第 10 步 plan 摘掉形态 / 视角，换了人的视角记进历史），
   以及服务端整步脚手架落进视图状态时保住只活在前端的内容。视图与同步层共用这一条边界。
   从 ws-snow-model.js 拆出（2026-09-29）；ws-snow-model.js 原样转出这里的全部名字。
   ========================================================== */

import { S2_STEPS, S2_STEP_DATA } from "./ws-snow-guide.js";
import { s2PovLabel, s2RosterList, s2SceneNo } from "./ws-snow-derive.js";

/* ---- 空白脚手架与缓存归一（视图与同步层共用的一条边界） ---- */
/* 所有作品（含新建）从空白十步开始 */
export function s2BlankScaffolds() {
  return {
    audience: { genre: "", reader: "", pleasure: "", source: "", exclude: "", emotion: "" },
    paragraph: { premiseF: "", premiseT: "", setup: "", d1: "", d2: "", d3: "", resolution: "" },
    characters: { sel: "c1", chars: { c1: { name: "", role: "主角", goal: "", ambition: "", values: "", conflict: "", epiphany: "", storyline: "", storyline_para: "" } } },
    planning: { sel: "", plans: {} },
    backstory: { sel: "c1", chars: { c1: { name: "", role: "主角", belief: "", wound: "", desire: "", fear: "", relation: "", povstory: "" } } },
    profile: { sel: "c1", chars: { c1: { name: "", role: "主角", physical: "", psych: "", environment: "", personality: "", contradiction: "", views: "" } } },
    scenes: { lines: [], list: [] },
    synopsis: { paras: { setup: "", d1: "", d2: "", d3: "", resolution: "" } },
    // 阶段 D：07 = 五段展开（05 的每一段扩成约一页，书里的第 6 步）+ 章节表（分章真相）
    outline: { chapters: [], expansions: { setup: "", d1: "", d2: "", d3: "", resolution: "" } },
  };
}
export function s2DefaultDrafts() { return Object.fromEntries(S2_STEPS.map(s => [s.key, ""])); }
export function s2DefaultChecks() { return Object.fromEntries(S2_STEPS.map(s => [s.key, (((S2_STEP_DATA[s.key] || {}).guide || {}).checklist || []).map(() => false)])); }
export function s2DefaultStates() { return Object.fromEntries(S2_STEPS.map(s => [s.key, "todo"])); }
export const S2_PLAN_FIELDS = ["goal", "conflict", "setback", "reaction", "dilemma", "decision"];
export function s2MergeScaffolds(stored) {
  const base = s2BlankScaffolds();
  if (stored) Object.keys(base).forEach(k => {
    if (!stored[k]) return;
    base[k] = { ...base[k], ...stored[k] };
    if (base[k].chars && stored[k].chars) base[k].chars = { ...base[k].chars, ...stored[k].chars };
    if (k === "planning" && base[k].plans && stored[k].plans) base[k].plans = { ...base[k].plans, ...stored[k].plans };
  });
  // 没有选中场时落在第一份规划上
  if (!base.planning.sel) base.planning = { ...base.planning, sel: Object.keys(base.planning.plans || {})[0] || "" };
  return s2SettlePlanning(base);
}

/* 第 10 步的 plan 里不存形态（mode）与视角（pov）：两者只有 09 场景行这一个家。
   以前 10 的编辑器把渲染时的默认值（mode、pov、空篇幅……）连同改动的那一格一起写进 plan，水合与 AI 生成
   也把服务端的 primary_form / pov_character_id 反推进 plan；上行时 plan.mode / plan.pov 优先于 09 的行——
   在 09 把一场改成反应场（或换了视角）之后，第 10 步的下一次上行又把服务端改回去（F02-01）。
   这里在每条进入视图状态的路上把两者从 plan 里摘掉；plan 带着视角而 09 那一行还没有视角时，视角挪进
   09 的行（作者或模型给过的值不丢）。没有可摘的键时原样返回同一个对象。 */
export function s2SettlePlanning(scaffolds) {
  const planning = scaffolds && scaffolds.planning;
  const plans = planning && planning.plans;
  if (!plans || !Object.values(plans).some(p => p && typeof p === "object" && ("mode" in p || "pov" in p))) return scaffolds;
  const povFor = {};
  const nextPlans = {};
  Object.entries(plans).forEach(([id, p]) => {
    if (!p || typeof p !== "object") { nextPlans[id] = p; return; }
    const rest = { ...p };
    delete rest.mode; delete rest.pov;
    nextPlans[id] = rest;
    if (p.pov) povFor[id] = p.pov;
  });
  const scenes = scaffolds.scenes || {};
  const list = Array.isArray(scenes.list) ? scenes.list : [];
  const nextList = list.map(s => (s && !s.pov && povFor[s.id]) ? { ...s, pov: povFor[s.id] } : s);
  const out = { ...scaffolds, planning: { ...planning, plans: nextPlans } };
  if (nextList.some((s, i) => s !== list[i])) out.scenes = { ...scenes, list: nextList };
  return out;
}

/* s2SettlePlanning 会让哪几场的视角换了人：plan 里记着视角，09 那一行却记的是另一个人（plan 的被丢掉），
   或者 09 那一行没填（plan 的挪进去）。按 09 的行序返回 [{ id, index, planPov, rowPov }]，rowPov 为空 = 挪进 09。
   与 09 相同的旧值（旧版第 10 步把渲染时的默认值冻进了 plan）不算。 */
export function s2PlanPovChanges(scaffolds) {
  const plans = ((scaffolds && scaffolds.planning) || {}).plans || {};
  const list = ((scaffolds && scaffolds.scenes) || {}).list;
  if (!Array.isArray(list)) return [];
  const out = [];
  list.forEach((s, index) => {
    const p = s && plans[s.id];
    const planPov = (p && typeof p === "object" && p.pov) ? String(p.pov) : "";
    const rowPov = (s && s.pov) ? String(s.pov) : "";
    if (planPov && planPov !== rowPov) out.push({ id: s.id, index, planPov, rowPov });
  });
  return out;
}

/* 读缓存时 plan 里旧存的视角收归 09（F02-01），换了人的那几场不能悄悄没了：给历史一条「视角统一到 09」，
   写明每一场第 10 步原来记的是谁、现在按谁。有 09 原来没填、从第 10 步挪进去的，附一份 09 挪之前的快照——
   不想要就在「历史」里回滚 09。scaffolds 是归一之前的样子（读进来的缓存）；没有换人的场时返回 null。 */
export function s2PovSettleEntry(scaffolds, drafts, t) {
  const changes = s2PlanPovChanges(scaffolds);
  if (!changes.length) return null;
  const roster = s2RosterList(scaffolds);
  const name = (pov) => s2PovLabel(pov, roster);
  const parts = changes.map(c => {
    const no = s2SceneNo(c.id, c.index);
    return c.rowPov ? `${no} 第 10 步记的是「${name(c.planPov)}」，按 09 的「${name(c.rowPov)}」` : `${no} 09 没填，用第 10 步的「${name(c.planPov)}」`;
  });
  const shown = parts.slice(0, 6).join("；") + (parts.length > 6 ? `；另有 ${parts.length - 6} 场` : "");
  let snap = null;
  if (changes.some(c => !c.rowPov)) {
    try { snap = JSON.parse(JSON.stringify({ draft: (drafts && drafts.scenes) || "", scaffold: { ...s2BlankScaffolds().scenes, ...scaffolds.scenes } })); } catch (e) { snap = null; }
  }
  return { t: t || Date.now(), who: "系统", action: "视角统一到 09", note: `视角只在 09 场景列表里定：${shown}`, key: "scenes", snap };
}

/* 只活在前端脚手架里的内容（F02-02）：09 的线索（lines，含每条线的「折射道德前提」）、每场挂在哪条线上
   （line），03 的错误信念（premiseF）。它们不进规范草稿，服务端回来的整步草稿（AI 生成、填入教练改写）
   反推出来的脚手架里没有它们——以前整步替换就把它们一并抹掉。这里把旧脚手架里的这几样接到新脚手架上：
   线索表新稿为空时沿用旧表；一场按 id 还在、它原来挂的线也还在，就挂回原来的线。 */
export function s2PreserveFeOnly(key, prev, next) {
  if (!prev || !next || typeof next !== "object") return next;
  if (key === "paragraph") {
    const premiseF = String(prev.premiseF || "");
    if (!premiseF.trim() || String(next.premiseF || "").trim()) return next;
    // 只写了错误信念时，canon 的 moral_premise 就是它——回来的 premiseT 是它的回声，不是作者写的真信念
    const echo = !String(prev.premiseT || "").trim() && String(next.premiseT || "").trim() === premiseF.trim();
    return { ...next, premiseF, ...(echo ? { premiseT: "" } : {}) };
  }
  if (key === "scenes") {
    const prevLines = Array.isArray(prev.lines) ? prev.lines : [];
    const lines = Array.isArray(next.lines) && next.lines.length ? next.lines : prevLines;
    const lineIds = new Set(lines.map(l => l && l.id));
    const prevLineOf = Object.fromEntries((prev.list || []).filter(s => s && s.id).map(s => [s.id, s.line]));
    const list = (next.list || []).map(s => {
      const line = s && prevLineOf[s.id];
      return (line && line !== s.line && lineIds.has(line)) ? { ...s, line } : s;
    });
    return { ...next, lines, list };
  }
  return next;
}

/* 服务端回来的整步脚手架落进视图状态：保住只活在前端的内容，并按第 10 步的规矩摘掉 plan 里的形态 / 视角 */
export function s2AdoptServerScaffold(scaffolds, key, next) {
  return s2SettlePlanning({ ...scaffolds, [key]: s2PreserveFeOnly(key, (scaffolds || {})[key], next) });
}
export function s2MergeChecks(stored) {
  const base = s2DefaultChecks();
  if (stored) Object.keys(base).forEach(k => { if (Array.isArray(stored[k]) && stored[k].length === base[k].length) base[k] = stored[k]; });
  return base;
}

/* 把后端水合/结构化导入的稀疏缓存折成视图真正持久化的完整形状。
   同步层和视图层共用这一条边界，避免“刚批准的服务端真相”因为 React 补齐空脚手架
   而被误判为作者编辑，再写回成 pending_review。 */
export function s2NormalizeState(saved) {
  const source = { ...(saved || {}) };
  return {
    ...source,
    drafts: { ...s2DefaultDrafts(), ...(source.drafts || {}) },
    scaffolds: s2MergeScaffolds(source.scaffolds),
    checks: s2MergeChecks(source.checks),
    states: { ...s2DefaultStates(), ...(source.states || {}) },
    history: Array.isArray(source.history) ? source.history : [],
    _t: source._t || Date.now(),
  };
}
