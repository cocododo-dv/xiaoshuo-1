/* ==========================================================
   雪花十步 · 一步的分步文本（叶子模块：只 import 步骤文案 ws-snow-guide.js、纯推导 ws-snow-derive.js
   与章名规则 labels/catalog.js）
   ----------------------------------------------------------
   「导出大纲」、「引用上下文」、回滚预览与「服务器上保存的版本」给作者看的是这一份：每一栏带着编辑器里的名字，
   一行一栏，角色 / 场景 / 章各自成组。以前它们用的是 s2Content——把脚手架里每一个字符串叶子按出现顺序拼起来，
   09 的行 id（row_<uuid>）、角色键（c1）、形态与线的枚举值（proactive / main）、第 10 步的 sel / rendering / length
   一股脑印进雪花大纲.md 和引用卡片（审计 F02-12，批准 #18b）。s2Content 仍留给「这一步是不是还空着」那种比较。

   s2StepLines(key, draft, scaffold, refs) → [{ label, text, depth, head }]：label 是栏名（可空），depth 0 是一组的
   抬头（head）或独立的一栏、1 是组里的一栏。refs 是整份脚手架（视角名与 04 名册、第 10 步按 09 的场序都从这里取）。
   s2StepText 拼成纯文本（组内缩两格），s2StepMarkdown 拼成 Markdown 列表（导出用，渲染出来也是一行一栏）。
   没写任何东西的一步给空数组 / 空串；脚手架是空的、却有一段旧的自由草稿时给那段草稿（一个字都不丢）。
   ========================================================== */

import {
  S2_AUD_FIELDS, S2_BACKSTORY_FIELDS, S2_BEATS, S2_CHAR_FIELDS, S2_LINE_KIND_LABEL, S2_PROFILE_FIELDS, S2_SYN_BEATS,
} from "./ws-snow-guide.js";
import { s2PovLabel, s2RosterList, s2SceneNo } from "./ws-snow-derive.js";
import { chapterNoInTitle } from "./labels/catalog.js";

/* 一栏的值：统一换行、去掉行尾空白与多余的空行 */
const clean = (v) => String(v == null ? "" : v).replace(/\r\n?/g, "\n").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
/* 一栏（值为空就没有这一栏） */
const field = (label, value, depth = 0) => { const text = clean(value); return text ? [{ label, text, depth }] : []; };
/* 一组的抬头（一个角色、一场、一张章表） */
const heading = (text) => ({ label: "", text, depth: 0, head: true });

/* 04 的价值观：脚手架里一行一条中间那截，读出来是书里的句式「没有什么比 ___ 更重要」 */
function fieldValue(fl, raw) {
  if (fl.kind !== "values") return raw;
  return String(raw || "").split("\n").map(s => s.trim()).filter(Boolean)
    .map(s => (s.includes(fl.suffix) ? s : `${fl.prefix}${s}${fl.suffix}`)).join("；");
}
const fieldsOf = (obj, fields, depth) => fields.flatMap(fl => field(fl.label, fieldValue(fl, (obj || {})[fl.f]), depth));

/* 01 读者定位 */
function audienceLines(sc) {
  return [...field("类型", sc.genre), ...fieldsOf(sc, S2_AUD_FIELDS, 0)];
}

/* 03 一段话概括：五句 + 道德前提的翻转 */
function paragraphLines(sc) {
  const out = S2_BEATS.flatMap(b => field(b.label, sc[b.f]));
  const wrong = clean(sc.premiseF);
  const right = clean(sc.premiseT);
  if (wrong && right) out.push({ label: "道德前提", text: `${wrong} → ${right}`, depth: 0 });
  else out.push(...field("道德前提（错误信念）", wrong), ...field("道德前提（正确信念）", right));
  return out;
}

/* 04 角色摘要表：一个角色一组（抬头写名字与定位），全书主角单列一行 */
function characterSheetLines(sc) {
  const chars = sc.chars || {};
  const out = [];
  const protagonist = clean(sc.protagonist);
  if (protagonist) out.push(...field("全书主角", clean((chars[protagonist] || {}).name) || "（名册里没有这个人）"));
  Object.keys(chars).forEach(id => {
    const c = chars[id] || {};
    const body = fieldsOf(c, S2_CHAR_FIELDS.filter(fl => fl.f !== "role"), 1);
    const name = clean(c.name);
    if (!name && !body.length) return;  // 空白角色（只有默认的「主角」定位）不算内容
    out.push(heading(`${name || "未命名角色"}${clean(c.role) ? `（${clean(c.role)}）` : ""}`), ...body);
  });
  return out;
}

/* 06 / 08：按 04 名册排人（名字与定位取 04），只列写了这一层深档的角色；不在名册里的旧角色排在后面 */
function characterDeepLines(sc, fields, refs) {
  const roster = ((refs && refs.characters) || {}).chars || {};
  const local = sc.chars || {};
  const ids = [...Object.keys(roster), ...Object.keys(local).filter(id => !roster[id])];
  const out = [];
  ids.forEach(id => {
    const body = fieldsOf(local[id], fields, 1);
    if (!body.length) return;
    const meta = roster[id] || local[id] || {};
    const name = clean(meta.name) || "未命名角色";
    out.push(heading(`${name}${clean(meta.role) ? `（${clean(meta.role)}）` : ""}${roster[id] ? "" : " · 不在 04 名册里"}`), ...body);
  });
  return out;
}

/* 05 一页梗概的五段 */
function synopsisLines(sc) {
  const paras = sc.paras || {};
  return S2_SYN_BEATS.flatMap(b => field(b.label, paras[b.f]));
}

/* 07：五段展开 + 章节表（分章结果的只读镜像）。章行「第 N 章 · 章名（灾一）：章摘要」，章名是系统占位时只写章号 */
function outlineLines(sc) {
  const expansions = sc.expansions || {};
  const out = S2_SYN_BEATS.flatMap(b => field(b.label, expansions[b.f]));
  const chapters = (Array.isArray(sc.chapters) ? sc.chapters : []).filter(c => c && typeof c === "object");
  if (chapters.length) {
    out.push(heading("章节表"));
    chapters.forEach((c, i) => {
      const title = clean(c.title);
      const name = `第 ${i + 1} 章${chapterNoInTitle(title, i) ? "" : ` · ${title}`}${clean(c.spine) ? `（${clean(c.spine)}）` : ""}`;
      const summary = clean(c.summary);
      out.push({ label: "", text: summary ? `${name}：${summary}` : name, depth: 1 });
    });
  }
  return out;
}

/* 09 场景列表：先列主线之外的线索（含「折射道德前提」），再一场一组——抬头「S01 · 主动 · 视角 某人 · 地点 · 灾一」 */
function sceneListLines(sc, refs) {
  const list = Array.isArray(sc.list) ? sc.list : [];
  const lines = Array.isArray(sc.lines) ? sc.lines : [];
  const roster = s2RosterList(refs);
  const out = [];
  const extra = lines.filter(l => l && l.kind !== "main");
  if (extra.length) {
    out.push(heading("线索"));
    extra.forEach(l => {
      const refract = clean(l.refract);
      out.push({ label: "", text: `${S2_LINE_KIND_LABEL[l.kind] || "支线"}「${clean(l.name) || "未命名"}」${refract ? `：折射道德前提——${refract}` : ""}`, depth: 1 });
    });
  }
  list.forEach((s, i) => {
    if (!s) return;
    const bits = [s2SceneNo(s.id, i), s.type === "reactive" ? "反应" : "主动"];
    const pov = s2PovLabel(s.pov, roster);
    if (pov) bits.push(`视角 ${pov}`);
    if (clean(s.place)) bits.push(clean(s.place));
    if (clean(s.spine)) bits.push(clean(s.spine));
    const line = s.line && s.line !== "main" ? extra.find(l => l.id === s.line) : null;
    out.push(heading(bits.join(" · ")),
      ...field("事件", s.event, 1), ...field("坩埚", s.crucible, 1), ...field("功能", s.fn, 1),
      ...(line ? field("线索", line.name, 1) : []), ...field("所在章", s.chapter, 1));
  });
  return out;
}

/* 10 场景规划：按 09 的场序，一场一组（只列规划过的场）；三拍先写这一场的形态那一组，接着的次要三拍随后 */
const BEAT_LABEL = { goal: "目标", conflict: "冲突", setback: "挫败", reaction: "反应", dilemma: "两难", decision: "决定" };
const PROACTIVE_BEATS = ["goal", "conflict", "setback"];
const REACTIVE_BEATS = ["reaction", "dilemma", "decision"];
const LENGTH_LABEL = { short: "短", medium: "中", long: "长" };
function planLines(plan, reactive, roster) {
  const p = plan || {};
  const beats = reactive ? [...REACTIVE_BEATS, ...PROACTIVE_BEATS] : [...PROACTIVE_BEATS, ...REACTIVE_BEATS];
  const out = beats.flatMap(f => field(BEAT_LABEL[f], p[f], 1));
  out.push(...field("代价", p.cost_requirement, 1));
  const onstage = (Array.isArray(p.onstage) ? p.onstage : []).map(id => s2PovLabel(id, roster)).filter(Boolean).join("、");
  out.push(...field("在场人物", onstage, 1), ...field("故事时间", p.story_time, 1), ...field("读者应感到", p.reader_emotion, 1),
    ...field("离场变化", p.exit_change, 1), ...field("钩子", p.hook, 1), ...field("必须出现", p.must_include, 1));
  if (p.rendering === "summary") out.push({ label: "呈现", text: "概述两段", depth: 1 });
  else if (p.rendering === "skip" && reactive) out.push({ label: "呈现", text: "略过（反应 / 两难 / 决定照样带给下一场）", depth: 1 });
  const length = clean(p.length);
  if (length && p.rendering !== "summary") out.push({ label: "篇幅", text: LENGTH_LABEL[length] || `${length} 字`, depth: 1 });
  out.push(...field("破例理由", p.exception, 1));
  return out;
}
function planningLines(sc, refs) {
  const plans = sc.plans || {};
  const list = (((refs && refs.scenes) || {}).list || []).filter(Boolean);
  const roster = s2RosterList(refs);
  const rows = list.length ? list : Object.keys(plans).map(id => ({ id }));
  const out = [];
  rows.forEach((s, i) => {
    const body = planLines(plans[s.id], s.type === "reactive", roster);
    if (!body.length) return;
    const title = clean((plans[s.id] || {}).title) || clean(s.event);
    out.push(heading([s2SceneNo(s.id, i), s.type === "reactive" ? "反应" : "主动", title].filter(Boolean).join(" · ")), ...body);
  });
  return out;
}

export function s2StepLines(key, draft, scaffold, refs) {
  const sc = scaffold && typeof scaffold === "object" && !Array.isArray(scaffold) ? scaffold : null;
  let out = [];
  if (sc) {
    if (key === "audience") out = audienceLines(sc);
    else if (key === "paragraph") out = paragraphLines(sc);
    else if (key === "characters") out = characterSheetLines(sc);
    else if (key === "synopsis") out = synopsisLines(sc);
    else if (key === "backstory") out = characterDeepLines(sc, S2_BACKSTORY_FIELDS, refs);
    else if (key === "outline") out = outlineLines(sc);
    else if (key === "profile") out = characterDeepLines(sc, S2_PROFILE_FIELDS, refs);
    else if (key === "scenes") out = sceneListLines(sc, refs);
    else if (key === "planning") out = planningLines(sc, refs);
  }
  if (!out.length) {
    // 02 一句话概括是自由文本；结构化步骤的脚手架空着、却还留着一段旧的自由草稿时，把那段给出来
    const text = clean(draft);
    if (text) out = [{ label: "", text, depth: 0 }];
  }
  return out;
}

const labelled = (l) => (l.label ? `${l.label}：${l.text}` : l.text);

/* 纯文本：一行一栏，组里的栏缩两格（引用上下文、回滚预览、服务器版本预览） */
export function s2StepText(key, draft, scaffold, refs) {
  return s2StepLines(key, draft, scaffold, refs)
    .map(l => { const pad = l.depth ? "  " : ""; return pad + labelled(l).replace(/\n/g, `\n${pad}`); })
    .join("\n");
}

/* Markdown 列表（导出大纲）：一栏一个列表项、栏名加粗，组抬头加粗、组里的栏缩一级；一栏里的换行留在列表项里 */
export function s2StepMarkdown(key, draft, scaffold, refs) {
  return s2StepLines(key, draft, scaffold, refs)
    .map(l => {
      const pad = l.depth ? "  " : "";
      const body = l.head ? `**${l.text}**` : (l.label ? `**${l.label}**：${l.text}` : l.text);
      return `${pad}- ${body.replace(/\n/g, `\n${pad}  `)}`;
    })
    .join("\n");
}
