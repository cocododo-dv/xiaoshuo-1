import { S2_BE_STEPS, s2NormalizeState } from "./ws-snow-model.js";

/* ==========================================================
   雪花构思 · 前端形状 ↔ 规范草稿（纯函数；2026-09-29 从 ws-snow-sync.jsx 拆出）
   · canonFromFE / feFromCanon：视图的脚手架形状（drafts / scaffolds）与后端规范字段的双向映射；
   · mergeCanon / applyCanonPatch：上行时的「作者主权」合并、教练补丁的「咨询式」合并；
   · buildStepFragmentFrom / stepSig：上行片段与去重签名；stepIsPristine：水合闸门的空白步判定。
   不读 store、不发请求、不碰 localStorage。
   ========================================================== */

// G5：FE→BE 步骤键映射统一以 ws-snow-model 的 S2_BE_STEPS 为正源（避免双份漂移；那是一个不 import 任何东西的叶子模块，视图因此能直接 import 本模块）
export const SNOW_STEPS = S2_BE_STEPS;
export const FE_BY_BE = Object.fromEntries(SNOW_STEPS.map(([fe, be]) => [be, fe]));
export const BE_BY_FE = Object.fromEntries(SNOW_STEPS);

export const snowCacheKey = (workId) => "ws_snow_state_v2::" + workId;

/* ---------- FE → BE：规范字段（喂完备性闸门 / scene plans 同步） ---------- */
const txt = (v) => (typeof v === "string" ? v.trim() : "");
/* 阶段 D：价值观按书里的句式「没有什么比___更重要」存规范值（一条一元素）；
   脚手架只存中间那截，换行分隔一行一条。已经是整句的（含「更重要」）原样上行。 */
const VALUE_RE = /^没有什么比(.+?)更重要[。．.!！]?$/;
const valuesToCanon = (raw) => String(raw || "").split("\n").map(s => s.trim()).filter(Boolean)
  .map(s => (s.includes("更重要") ? s : `没有什么比${s}更重要`));
const valuesFromCanon = (arr) => (Array.isArray(arr) ? arr : (arr ? [arr] : []))
  .map(v => { const s = String(v || "").trim(); const m = VALUE_RE.exec(s); return m ? m[1].trim() : s; })
  .filter(Boolean).join("\n");
/* 07 历史草稿的 paragraphs 是「NN 章名：一句话（灾一）」的章行镜像；阶段 D 起是五段展开的散文。 */
const OUTLINE_LINE_RE = /^(\d+)\s+([^：:]+)[：:]?(.*)$/;
const isChapterMirror = (para) => {
  const lines = String(para || "").split("\n").map(x => x.trim()).filter(Boolean);
  return lines.length > 0 && lines.every(l => OUTLINE_LINE_RE.test(l));
};
function canonFromFE(feKey, saved) {
  const sc = ((saved || {}).scaffolds || {})[feKey] || {};
  const draftText = txt(((saved || {}).drafts || {})[feKey]);
  if (feKey === "audience") {
    return {
      category: txt(sc.genre), target_reader: txt(sc.reader), delight_reason: txt(sc.pleasure),
      story_kind: txt(sc.source), genre_promise: txt(sc.exclude), expected_reader_emotion: txt(sc.emotion),
      // 阶段 J：全书的叙述人称与时态——起草时有约束力
      narrative_stance: txt(sc.stance),
    };
  }
  if (feKey === "logline") return { summary: draftText.split("\n").filter(Boolean)[0] || "" };
  if (feKey === "paragraph") {
    return {
      sentences: [txt(sc.setup), txt(sc.d1), txt(sc.d2), txt(sc.d3), txt(sc.resolution)],
      moral_premise: txt(sc.premiseT) || txt(sc.premiseF),
    };
  }
  if (feKey === "characters") {
    return {
      // 阶段 L：全书主角（挫折以此人衡量）——旧缓存没有这个键时不上行，不清服务端 / AI 给的值
      ...(sc.protagonist != null ? { protagonist_character_id: txt(sc.protagonist) } : {}),
      characters: Object.entries(sc.chars || {}).map(([id, c]) => ({
      character_id: id, display_name: txt(c.name), role: txt(c.role), goal: txt(c.goal),
      ambition: txt(c.ambition), values: valuesToCanon(c.values), conflict: txt(c.conflict), epiphany: txt(c.epiphany),
      // 阶段 D：每个角色自己的一句话 / 一段话故事线（书里角色表的两栏）。旧缓存没有这两个键时不上行——
      // mergeCanon 只在 FE 键缺席时保服务端值，AI 生成过的故事线不会被一次自动保存清空。
      ...(c.storyline != null ? { one_sentence_summary: txt(c.storyline) } : {}),
      ...(c.storyline_para != null ? { one_paragraph_summary: txt(c.storyline_para) } : {}),
    })) };
  }
  if (feKey === "synopsis") {
    const p = sc.paras || {};
    return { paragraphs: [txt(p.setup), txt(p.d1), txt(p.d2), txt(p.d3), txt(p.resolution)] };
  }
  if (feKey === "backstory") {
    return { characters: Object.entries(sc.chars || {}).map(([id, c]) => ({
      character_id: id, display_name: txt(c.name), role: txt(c.role),
      synopsis: [c.belief && `信念：${txt(c.belief)}`, c.wound && `旧伤：${txt(c.wound)}`, c.desire && `欲望：${txt(c.desire)}`, c.fear && `恐惧：${txt(c.fear)}`, c.relation && `关系：${txt(c.relation)}`,
        // 阶段 D：第六行——从这个角色的视角讲整本书（书里的第 5 步）
        c.povstory && `视角故事：${txt(c.povstory)}`].filter(Boolean).join("\n"),
    })) };
  }
  if (feKey === "outline") {
    /* P2：章表是结构化字段 chapters，物化分章读的是它。
       阶段 D：paragraphs 回到书里的第 6 步——五段展开（05 的每一段扩成约一页），
       不再用章行的文本镜像去覆盖它。 */
    const ex = sc.expansions || {};
    return {
      paragraphs: [txt(ex.setup), txt(ex.d1), txt(ex.d2), txt(ex.d3), txt(ex.resolution)],
      chapters: (sc.chapters || []).map((c, i) => ({
        row_uid: txt(c.row_uid), chapter_seq: i + 1, act: c.act || 1,
        title: txt(c.title), summary: txt(c.summary), spine: txt(c.spine), chapter_goal: txt(c.goal),
      })),
    };
  }
  if (feKey === "profile") {
    return { characters: Object.entries(sc.chars || {}).map(([id, c]) => ({
      character_id: id, display_name: txt(c.name), role: txt(c.role),
      physical_profile: { appearance: txt(c.physical) },
      personality_profile: { strongest_trait: txt(c.personality) },
      environment_profile: { home: txt(c.environment) },
      psychological_profile: { philosophy: txt(c.views), self_image: txt(c.contradiction), deepest_fear: txt(c.psych) },
    })) };
  }
  if (feKey === "scenes") {
    return { scenes: (sc.list || []).map((s, i) => ({
      row_uid: s.id || `S${String(i + 1).padStart(2, "0")}`, scene_seq: i + 1,
      summary: txt(s.event), primary_form: s.type === "reactive" ? "reactive" : "proactive",
      pov_character_id: txt(s.pov), location: txt(s.place), crucible: txt(s.crucible), chapter_role: txt(s.fn),
      // P2：脊柱标记（灾一/灾二/灾三）以前从不上行、水合还硬写回 ""，作者标完一刷新就丢。
      // 分章的锚点靠它，必须往返保真。
      spine: txt(s.spine),
    })) };
  }
  if (feKey === "planning") {
    const listScenes = (((saved || {}).scaffolds || {}).scenes || {}).list || [];
    const plans = sc.plans || {};
    return { scenes: listScenes.map((s, i) => {
      const plan = plans[s.id] || {};
      const form = (plan.mode || (s.type === "reactive" ? "reactive" : "proactive"));
      return {
        row_uid: s.id || `S${String(i + 1).padStart(2, "0")}`, summary: txt(s.event),
        // 阶段 R：场景题名只在作者写了时上行——以前每次保存都拿 09 的事件文本覆盖服务端（模型）给的短题名
        ...(txt(plan.title) ? { title: txt(plan.title) } : {}),
        primary_form: form, location: txt(s.place), crucible: txt(s.crucible), scene_crucible: txt(s.crucible), spine: txt(s.spine),
        pov_character_id: txt(plan.pov) || txt(s.pov),
        goal: txt(plan.goal), conflict: txt(plan.conflict), setback: txt(plan.setback),
        reaction: txt(plan.reaction), dilemma: txt(plan.dilemma), decision: txt(plan.decision),
        cost_requirement: txt(plan.cost_requirement),
        // 阶段 J：原著的几栏——在场人物（角色 id 列表）、故事时间、读者应感到
        onstage_chars_json: Array.isArray(plan.onstage) ? plan.onstage.map(txt).filter(Boolean) : [],
        story_time: txt(plan.story_time), expected_reader_emotion: txt(plan.reader_emotion),
        // 阶段 M：钩子与离场变化——后端一直有这两列，前端此前没有输入框，文案却承诺「钩子」
        hook: txt(plan.hook), exit_change: txt(plan.exit_change),
        // 阶段 R：篇幅带 / 必须出现 / 破例理由——旧本地缓存没有这些键时不上行，服务端（模型）给的值不被抹掉
        ...(plan.length !== undefined ? { target_length_band: txt(plan.length) || "medium" } : {}),
        ...(plan.must_include !== undefined ? { must_include_text: txt(plan.must_include) } : {}),
        ...(plan.exception !== undefined ? { exception_reason: txt(plan.exception) } : {}),
        // 阶段 C / I / N：呈现方式——概述对两种形态都合法（原著第 1 场是主动场的叙述概述），略过只给反应场
        rendering_mode: plan.rendering === "summary" ? "summary" : (form === "reactive" && plan.rendering === "skip") ? "skip" : "full",
      };
    }) };
  }
  return {};
}

/* ---------- BE → FE：fe_* 缺席时从规范字段反推原型形状 ---------- */
function feFromCanon(feKey, draft) {
  const d = draft || {};
  const pad2 = (n) => String(n).padStart(2, "0");
  if (feKey === "audience") {
    return { scaffold: { genre: d.category || "", reader: d.target_reader || "", pleasure: d.delight_reason || "", source: d.story_kind || "", exclude: d.genre_promise || "", emotion: d.expected_reader_emotion || "", stance: d.narrative_stance || "" } };
  }
  if (feKey === "logline") return { text: d.summary || "" };
  if (feKey === "paragraph") {
    const s = d.sentences || [];
    return { scaffold: { premiseF: "", premiseT: d.moral_premise || "", setup: s[0] || "", d1: s[1] || "", d2: s[2] || "", d3: s[3] || "", resolution: s[4] || "" } };
  }
  if (feKey === "characters" || feKey === "backstory" || feKey === "profile") {
    const chars = {};
    (d.characters || []).forEach((c, i) => {
      const id = c.character_id || "c" + (i + 1);
      if (feKey === "characters") {
        chars[id] = {
          name: c.display_name || "", role: c.role || "主角", goal: c.goal || "", ambition: c.ambition || "",
          values: valuesFromCanon(c.values), conflict: c.conflict || "", epiphany: c.epiphany || "",
          storyline: c.one_sentence_summary || "", storyline_para: c.one_paragraph_summary || "",
        };
      } else if (feKey === "backstory") {
        // 往返保真：canonFromFE 打包成「信念：…\n旧伤：…」的前缀行，这里拆回六个字段；
        // AI/自由文本没有前缀时整段进「视角故事」（阶段 D：没有前缀的整段角色梗概就是
        // 书里第 5 步的视角故事，不是信念），续行跟随最近一个前缀字段。
        const bk = { name: c.display_name || "", role: c.role || "主角", belief: "", wound: "", desire: "", fear: "", relation: "", povstory: "" };
        const prefixMap = { "信念": "belief", "旧伤": "wound", "欲望": "desire", "恐惧": "fear", "关系": "relation", "视角故事": "povstory" };
        let cursor = null, plain = [];
        String(c.synopsis || "").split("\n").forEach(line => {
          const m = /^(信念|旧伤|欲望|恐惧|关系|视角故事)[：:]\s*(.*)$/.exec(line.trim());
          if (m) { cursor = prefixMap[m[1]]; bk[cursor] = bk[cursor] ? bk[cursor] + "\n" + m[2] : m[2]; }
          else if (cursor) bk[cursor] += (line.trim() ? "\n" + line : "");
          else if (line.trim()) plain.push(line);
        });
        if (plain.length) bk.povstory = (plain.join("\n") + (bk.povstory ? "\n" + bk.povstory : ""));
        chars[id] = bk;
      } else {
        chars[id] = {
          name: c.display_name || "", role: c.role || "主角",
          physical: (c.physical_profile || {}).appearance || "", psych: (c.psychological_profile || {}).deepest_fear || "",
          environment: (c.environment_profile || {}).home || "", personality: (c.personality_profile || {}).strongest_trait || "",
          contradiction: (c.psychological_profile || {}).self_image || "", views: (c.psychological_profile || {}).philosophy || "",
        };
      }
    });
    const sel = Object.keys(chars)[0] || "c1";
    // 阶段 L：04 的全书主角随名册一起水合（06 / 08 没有这个键）
    return { scaffold: { sel, chars, ...(feKey === "characters" ? { protagonist: d.protagonist_character_id || "" } : {}) } };
  }
  if (feKey === "synopsis") {
    const p = d.paragraphs || [];
    return { scaffold: { paras: { setup: p[0] || "", d1: p[1] || "", d2: p[2] || "", d3: p[3] || "", resolution: p[4] || "" } } };
  }
  if (feKey === "outline") {
    // 阶段 D：paragraphs 是五段展开（05 的每一段扩成约一页）；历史草稿里的章行镜像不是展开文，水合成空槽
    const paras = Array.isArray(d.paragraphs) ? d.paragraphs : [];
    const slot = (i) => (isChapterMirror(paras[i]) ? "" : String(paras[i] || ""));
    const expansions = { setup: slot(0), d1: slot(1), d2: slot(2), d3: slot(3), resolution: slot(4) };
    // 结构化 chapters 优先（P2 新契约，无损）；缺席时才回退解析文本行（历史草稿 / 旧 LLM 输出）
    if (Array.isArray(d.chapters) && d.chapters.length) {
      return { scaffold: { expansions, chapters: d.chapters.map((c, i) => ({
        row_uid: c.row_uid || "", id: pad2(i + 1), act: Math.min(Math.max(c.act || 1, 1), 3),
        title: c.title || "", summary: c.summary || "", spine: c.spine || "", goal: c.chapter_goal || "",
      })) } };
    }
    // 回退只认真正的章行（与后端 parse_outline_chapters 同一纪律）：散文段落解析不出章，绝不造假章
    const chapters = [];
    paras.forEach((para, ai) => {
      String(para || "").split("\n").map(x => x.trim()).filter(Boolean).forEach(line => {
        const m = OUTLINE_LINE_RE.exec(line);
        if (!m) return;
        chapters.push({ id: m[1], act: Math.min(ai + 1, 3), title: m[2].trim(), summary: m[3].replace(/（.*?）$/, "").trim(), spine: /灾[一二三]/.test(line) ? (line.match(/灾[一二三]/) || [""])[0] : "" });
      });
    });
    return { scaffold: { expansions, chapters } };
  }
  if (feKey === "scenes") {
    return { scaffold: { lines: [], list: (d.scenes || []).map((s, i) => ({
      id: s.row_uid || "S" + pad2(i + 1), type: s.primary_form === "reactive" ? "reactive" : "proactive", line: "main",
      pov: s.pov_character_id || "", place: s.location || "", event: s.summary || "", crucible: s.crucible || "", fn: s.chapter_role || "",
      spine: s.spine || "",
      // 阶段 M：章归属只读展示（分章面板才是编辑处；章号退化值不显示）
      chapter: (s.chapter_title && s.chapter_title !== s.chapter_id) ? s.chapter_title : "",
    })) } };
  }
  if (feKey === "planning") {
    const plans = {};
    (d.scenes || []).forEach((s, i) => {
      plans[s.row_uid || "S" + pad2(i + 1)] = {
        mode: s.primary_form === "reactive" ? "reactive" : "proactive", pov: s.pov_character_id || "",
        goal: s.goal || "", conflict: s.conflict || "", setback: s.setback || "",
        reaction: s.reaction || "", dilemma: s.dilemma || "", decision: s.decision || "",
        cost_requirement: s.cost_requirement || "",
        onstage: Array.isArray(s.onstage_chars_json) ? s.onstage_chars_json.filter(Boolean) : [],
        story_time: s.story_time || "", reader_emotion: s.expected_reader_emotion || "",
        hook: s.hook || "", exit_change: s.exit_change || "",
        rendering: (s.rendering_mode === "summary" || s.rendering_mode === "skip") ? s.rendering_mode : "full",
        // 阶段 R：题名只在与摘要不同（模型 / 作者另起的短题名）时水合，等于摘要时留空 = 跟随 09 的事件
        title: (s.title && s.title !== s.summary) ? s.title : "",
        length: s.target_length_band || "", must_include: s.must_include_text || "", exception: s.exception_reason || "",
      };
    });
    return { scaffold: { sel: Object.keys(plans)[0] || "", plans } };
  }
  return {};
}

function canonHasContent(feKey, draft) {
  const d = draft || {};
  if (feKey === "logline") return !!txt(d.summary);
  if (feKey === "audience") return !!(txt(d.category) || txt(d.target_reader) || txt(d.delight_reason));
  if (feKey === "paragraph") return (d.sentences || []).some(s => txt(s)) || !!txt(d.moral_premise);
  if (feKey === "synopsis") return (d.paragraphs || []).some(s => txt(s));
  // 阶段 D：07 只有章表、五段展开还空着，也算服务端有内容（否则水合会跳过它）
  if (feKey === "outline") return (d.paragraphs || []).some(s => txt(s)) || (Array.isArray(d.chapters) && d.chapters.length > 0);
  if (feKey === "characters" || feKey === "backstory" || feKey === "profile") return (d.characters || []).length > 0;
  return (d.scenes || []).length > 0;
}

// 阶段 E：后端 stale = 曾批准、上游又改了——前端态仍是「已确认」，「需复核」由 health 的
// beStatus / staleReason 驱动（失效的单一真相在后端；E3 第二步起前端没有第二套失效算法）。
const BE_STATE_TO_FE = { approved: "done", skipped: "skip", stale: "done" };

/* ---------- 规范字段保真层（AI 融合 F1） ----------
   后端 generate（结构化整步生成）产出的规范草稿比原型脚手架能表达的更富
   （角色圣经四维、期待读者情绪等）。为了让上行 PATCH 不再把这些富字段剪掉，
   这里维护一份「服务端规范草稿」内存镜像（hydrate / PATCH 回包 / 结构化采纳时刷新），
   push 时把脚手架派生的规范字段深合并在它之上：
   - 对象递归合并（FE 键覆盖，服务端独有键幸存——脚手架没有输入框的富字段靠这条活下来）；
   - 数组以 FE 成员与顺序为准（作者删除即删除），成员按 id 对位继承服务端富字段；
   - FE 出现的标量一律作者说了算（含清空为 ""），只有 FE 缺失(null/undefined)才保服务端值。 */
const stripFe = (draft) => {
  const out = {};
  Object.keys(draft || {}).forEach(k => { if (k.indexOf("fe_") !== 0) out[k] = draft[k]; });
  return out;
};
const CANON_ID_KEYS = ["character_id", "row_uid", "scene_id"];
function mergeCanon(server, fe) {
  if (Array.isArray(fe)) {
    if (!Array.isArray(server)) return fe;
    return fe.map(item => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return item;
      const idKey = CANON_ID_KEYS.find(k => item[k] != null && item[k] !== "");
      const match = idKey ? server.find(s => s && typeof s === "object" && s[idKey] === item[idKey]) : null;
      return match ? mergeCanon(match, item) : item;
    });
  }
  if (fe && typeof fe === "object") {
    if (!server || typeof server !== "object" || Array.isArray(server)) return fe;
    const out = { ...server };
    Object.keys(fe).forEach(k => { out[k] = mergeCanon(server[k], fe[k]); });
    return out;
  }
  if (fe == null && server != null) return server;
  return fe;
}

/* 咨询式补丁合并（教练 candidate_patch 用）：与 mergeCanon 的「FE 主权」语义相反——
   补丁是建议不是全量替换：空串/空值不清空既有内容；数组按 id 对位合并、
   不删除补丁里没提到的成员（membership 保持当前草稿）。 */
function applyCanonPatch(base, patch) {
  if (Array.isArray(patch)) {
    if (!Array.isArray(base)) return patch;
    const out = base.slice();
    patch.forEach(p => {
      if (!p || typeof p !== "object" || Array.isArray(p)) return;
      const idKey = CANON_ID_KEYS.find(k => p[k] != null && p[k] !== "");
      const i = idKey ? out.findIndex(b => b && typeof b === "object" && b[idKey] === p[idKey]) : -1;
      if (i >= 0) out[i] = applyCanonPatch(out[i], p);
      else out.push(p); // 补丁新增成员（如教练建议加一个镜像角色）
    });
    return out;
  }
  if (patch && typeof patch === "object") {
    const out = (base && typeof base === "object" && !Array.isArray(base)) ? { ...base } : {};
    Object.keys(patch).forEach(k => { out[k] = applyCanonPatch(out[k], patch[k]); });
    return out;
  }
  if ((patch === "" || patch == null) && base != null && base !== "") return base;
  return patch;
}

/* push 片段与去重签名：snowPushKey(上行) / snowHydrate(预填 lastPushed) /
   applyServerStep(结构化采纳) 共用，保证各侧 sig 计算完全一致——BUG-2 防回退的前提。
   serverCanon = 这一步的服务端规范草稿镜像（没有就 null）；按作品取镜像的包装在 ws-snow-sync-state.js。 */
function buildStepFragmentFrom(feKey, cache, serverCanon) {
  const c = cache || {};
  const canon = canonFromFE(feKey, c);
  const fragment = {
    ...(serverCanon ? mergeCanon(serverCanon, canon) : canon),
    fe_text: ((c.drafts || {})[feKey]) || "",
    fe_scaffold: ((c.scaffolds || {})[feKey]) || null,
    fe_checks: ((c.checks || {})[feKey]) || [],
    fe_state: ((c.states || {})[feKey]) || "todo",
    fe_t: c._t || Date.now(),
  };
  if (feKey === "audience") {
    // E3 第二步：fe_meta 只剩跨会话 journal；revs / confirmRevs 不再写穿（失效真相在后端）
    fragment.fe_meta = {
      history: (c.history || []).slice(0, 20).map(h => ({ t: h.t, who: h.who, action: h.action, note: h.note, key: h.key })),
    };
  }
  return fragment;
}
function stepSig(fragment) {
  const { fe_t, ...sigPart } = fragment;
  return JSON.stringify(sigPart);
}

/* ---------- 空白步判定（水合闸门用） ----------
   「从没动过的空白步」= 没有自由草稿、状态还是待写 / 进行中、脚手架里的非空叶子都是空白默认稿自带的
   （如角色步的 sel=c1 / role=主角）。它不是作者的编辑：水合时让位给服务端内容，上行时不拿去覆盖服务端。
   作者同步过之后再亲手清空的步骤不在此列——那一步在 lastPushed 里有账，照常上行。 */
let blankLeafSets = null;
function nonEmptyLeaves(value, path, out) {
  if (typeof value === "string") { if (value.trim()) out.push(`${path}=${value.trim()}`); }
  else if (typeof value === "number" || typeof value === "boolean") out.push(`${path}=${value}`);
  else if (Array.isArray(value)) value.forEach((item, i) => nonEmptyLeaves(item, `${path}.${i}`, out));
  else if (value && typeof value === "object") Object.keys(value).forEach(k => nonEmptyLeaves(value[k], `${path}.${k}`, out));
  return out;
}
function blankLeavesFor(feKey) {
  if (!blankLeafSets) {
    const blank = s2NormalizeState({});
    blankLeafSets = Object.fromEntries(SNOW_STEPS.map(([fe]) => [fe, new Set(nonEmptyLeaves((blank.scaffolds || {})[fe], "", []))]));
  }
  return blankLeafSets[feKey] || new Set();
}
function stepIsPristine(feKey, cache) {
  const c = cache || {};
  if (txt((c.drafts || {})[feKey])) return false;
  const st = (c.states || {})[feKey];
  if (st && st !== "todo" && st !== "active") return false;
  const blank = blankLeavesFor(feKey);
  return nonEmptyLeaves((c.scaffolds || {})[feKey], "", []).every(leaf => blank.has(leaf));
}

/* 规范草稿 → 可读文本（与视图 s2Content 同一折叠法：脚手架里的字符串按出现顺序拼接，跳过空串） */
function canonText(feKey, draft) {
  const fe = feFromCanon(feKey, draft || {});
  if (fe.text != null) return String(fe.text);
  const out = [];
  const walk = (v) => {
    if (typeof v === "string") out.push(v);
    else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  walk(fe.scaffold || {});
  return out.filter(s => s && s.trim()).join("\n");
}

export {
  txt, canonFromFE, feFromCanon, canonHasContent, BE_STATE_TO_FE, stripFe, mergeCanon, applyCanonPatch,
  buildStepFragmentFrom, stepSig, stepIsPristine, canonText,
};
