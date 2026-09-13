import { apiGet, apiPatch, apiPost } from "./lib/client.js";
import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { S2_BE_STEPS, s2NormalizeState } from "./ws-snow.jsx";

/* global window */
/* ==========================================================
   SnowSync — 雪花构思 ↔ snowflake-workspace v2（FE-ALIGN F3）
   ----------------------------------------------------------
   ws_snow_state_v2::<work> 退化为后端真相的写穿缓存：
   - 视图保存（ws:snow-saved）→ 按步 diff → PATCH steps/{key}
     （draft 同时带规范字段喂完备性闸门 + fe_* 键无损保存原型形状）；
     fe_state 变为 done 时顺手 POST approve（闸门不满足则静默跳过）。
   - 启动 / 进入 #construct / 切作品 → GET workspace 水合：
     fe_* 键优先（无损还原），无 fe_* 时从规范字段反推原型形状
     （真·雪花管线生成的项目）；本地 _t 不旧于
     服务端则本地为准（未上行的编辑不被覆盖）。
   - 失效真相在后端（E3 第二步已移除本地 revs/confirmRevs 图）；history（过程
     快照日志）留本地（体积大、跨会话价值低，账本记录）。
   ========================================================== */

// G5：FE→BE 步骤键映射统一以 ws-snow 的 S2_BE_STEPS 为正源（避免双份漂移）
const SNOW_STEPS = S2_BE_STEPS;
const FE_BY_BE = Object.fromEntries(SNOW_STEPS.map(([fe, be]) => [be, fe]));
const BE_BY_FE = Object.fromEntries(SNOW_STEPS);

const snowCacheKey = (workId) => "ws_snow_state_v2::" + workId;
const activeWork = () => { try { return (WsWorks && WsWorks.activeId()) || ""; } catch (e) { return ""; } };

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
    return { characters: Object.entries(sc.chars || {}).map(([id, c]) => ({
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
        row_uid: s.id || `S${String(i + 1).padStart(2, "0")}`, title: txt(s.event), summary: txt(s.event),
        primary_form: form, location: txt(s.place), crucible: txt(s.crucible), scene_crucible: txt(s.crucible), spine: txt(s.spine),
        pov_character_id: txt(plan.pov) || txt(s.pov),
        goal: txt(plan.goal), conflict: txt(plan.conflict), setback: txt(plan.setback),
        reaction: txt(plan.reaction), dilemma: txt(plan.dilemma), decision: txt(plan.decision),
        cost_requirement: txt(plan.cost_requirement),
        // 阶段 C：反应场的呈现方式（full / summary）。只对反应场上行；主动场服务端恒为 full。
        ...(form === "reactive" ? { rendering_mode: plan.rendering === "summary" ? "summary" : "full" } : {}),
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
    return { scaffold: { genre: d.category || "", reader: d.target_reader || "", pleasure: d.delight_reason || "", source: d.story_kind || "", exclude: d.genre_promise || "", emotion: d.expected_reader_emotion || "" } };
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
    return { scaffold: { sel, chars } };
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
        rendering: s.rendering_mode === "summary" ? "summary" : "full",
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
const snowCanon = {}; // workId -> feKey -> 服务端规范草稿（已剥 fe_*）
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
   applyServerStep(结构化采纳) 共用，保证各侧 sig 计算完全一致——BUG-2 防回退的前提。 */
function buildStepFragment(feKey, cache, workId) {
  const c = cache || {};
  const canon = canonFromFE(feKey, c);
  const serverCanon = workId ? ((snowCanon[workId] || {})[feKey] || null) : null;
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

/* ---------- 水合 ---------- */
const snowHydratedOnce = {};
const snowReadyFlags = {};
const snowUnsupported = {};
/* 后端 per-step 权威健康（score/status/gaps/completeness）：只读后端真相，
   与前端写穿缓存分开存（避免被本地 save 覆盖）。hydrate 时全量捕获，
   每次 update_step 的 PATCH 响应里带最新 step.health → 增量更新。 */
const snowHealth = {}; // workId -> feKey -> shaped health
/* 整份 workspace 回包 → 刷新所有步骤的权威健康。approve / accept-stale 的回包都带 workspace：
   批准上游会让下游按消费字段置 stale，这里顺手把它们的 stale 状态收进来，「需复核」不必等下一次全量水合。 */
function captureWorkspaceHealth(workId, ws) {
  if (!workId || !ws || !Array.isArray(ws.steps)) return false;
  const bucket = snowHealth[workId] || (snowHealth[workId] = {});
  ws.steps.forEach(step => { const feKey = FE_BY_BE[step && step.step_key]; if (feKey) bucket[feKey] = shapeStepHealth(step); });
  return true;
}
function shapeStepHealth(step) {
  const h = (step && step.health) || {};
  const comp = (step && step.completeness) || {};
  const arr = (v) => (Array.isArray(v) ? v : []);
  return {
    score: typeof h.score === "number" ? h.score : null,
    status: h.status || null,                       // pass / maybe / rewrite
    gaps: arr(h.gaps),
    nextActions: arr(h.next_actions),
    missingFields: arr(comp.missing_fields).length ? arr(comp.missing_fields) : arr(h.missing_fields),
    filled: typeof comp.filled_count === "number" ? comp.filled_count : null,
    total: typeof comp.total_count === "number" ? comp.total_count : null,
    gateSatisfied: !!(step && step.gate_satisfied),
    beStatus: (step && step.status) || null,        // draft / pending_review / approved / skipped / stale
    // 阶段 E：失效真相来自后端——原因、是否已确认仍有效、本版与本步确认时消费的上游版本（step_run_id）
    staleReason: (step && step.stale_reason) || "",
    staleAcceptedAt: (step && step.stale_accepted_at) || null,
    version: typeof (step && step.version) === "number" ? step.version : null,
    stepRunId: (step && step.artifact && step.artifact.step_run_id) || null,
    inputRefs: (step && step.artifact && step.artifact.input_refs && typeof step.artifact.input_refs === "object")
      ? { ...step.artifact.input_refs } : {},
  };
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

/* 物化后回流（FE 补接 resync）：workspace 回包自带 resync_status——物化过的场，
   9/10 步再改动后与目录场景卡（SceneCard）的 diff。pendingCount>0 = 构思领先于目录，
   写作台 / AI 起草台拿到的还是旧三拍。只读后端真相，与写穿缓存分开存。 */
const snowResync = {}; // workId -> { pendingCount, pendingScenes }
/* P2 分章现状（后端只读真相）：章数、未分章场数、是否已分完。随 workspace 回包更新。 */
const snowChapterStatus = {}; // workId -> chapter_plan_status
const snowSyncStates = {}; // workId -> 本机缓存 / 服务端写穿的诚实状态

function snowErrorShape(error, fallback, scope = "remote") {
  return {
    code: (error && (error.code || error.status)) || "SYNC_FAILED",
    message: (error && error.message) || fallback || "同步失败，请稍后重试",
    scope,
    offline: typeof navigator !== "undefined" && navigator.onLine === false,
  };
}

function setSnowSyncState(workId, patch) {
  if (!workId) return;
  const previous = snowSyncStates[workId] || {
    phase: "idle", pendingSteps: [], error: null, localSavedAt: null, lastSyncedAt: null,
  };
  snowSyncStates[workId] = { ...previous, ...patch };
  try { window.dispatchEvent(new CustomEvent("ws:snow-sync-state", { detail: { workId, state: snowSyncStates[workId] } })); } catch (e) {}
}

function readSnowSyncState(workId) {
  return snowSyncStates[workId] || {
    phase: "idle", pendingSteps: [], error: null, localSavedAt: null, lastSyncedAt: null,
  };
}
function shapeResync(ws) {
  const rs = (ws && ws.resync_status) || {};
  const scenes = Array.isArray(rs.pending_scenes) ? rs.pending_scenes : [];
  return {
    pendingCount: typeof rs.pending_count === "number" ? rs.pending_count : scenes.length,
    pendingScenes: scenes.map(s => ({
      scenePlanId: (s && s.scene_plan_id) || "",
      sceneId: (s && s.scene_id) || "",
      title: (s && s.title) || "",
      changedFields: Array.isArray(s && s.changed_fields) ? s.changed_fields : [],
    })),
  };
}
function captureResync(workId, ws) {
  if (!workId || !ws || !ws.resync_status) return;
  snowResync[workId] = shapeResync(ws);
  try { window.dispatchEvent(new CustomEvent("ws:snow-resync", { detail: workId })); } catch (e) {}
}

function captureChapterStatus(workId, ws) {
  if (!workId || !ws || !ws.chapter_plan_status) return;
  const s = ws.chapter_plan_status;
  snowChapterStatus[workId] = {
    chapterCount: Number(s.chapter_count) || 0,
    assignedSceneCount: Number(s.assigned_scene_count) || 0,
    unassignedSceneCount: Number(s.unassigned_scene_count) || 0,
    unassignedScenes: Array.isArray(s.unassigned_scenes) ? s.unassigned_scenes : [],
    chaptered: !!s.chaptered,
  };
  try { window.dispatchEvent(new CustomEvent("ws:snow-chapter-plan", { detail: workId })); } catch (e) {}
}

async function snowHydrate(workId, opts) {
  const force = !!(opts && opts.force);
  if (!workId || snowUnsupported[workId]) return;
  if (!force && snowHydratedOnce[workId]) return;
  snowHydratedOnce[workId] = true;
  let ws = null;
  try {
    ws = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace`);
  } catch (e) {
    if (e && (e.status === 409 || e.status === 404)) snowUnsupported[workId] = true;
    else delete snowHydratedOnce[workId]; // 网络类失败：下次再试
    if (!(e && (e.status === 409 || e.status === 404))) {
      setSnowSyncState(workId, { phase: "error", error: snowErrorShape(e, "无法读取服务器构思版本", "hydrate"), pendingSteps: [] });
    }
    return;
  }
  const priorSyncState = readSnowSyncState(workId);
  if (priorSyncState.phase === "idle" || (priorSyncState.phase === "error" && priorSyncState.error && priorSyncState.error.scope === "hydrate")) {
    setSnowSyncState(workId, { phase: "synced", error: null, lastSyncedAt: Date.now() });
  }
  snowReadyFlags[workId] = !!(ws && ws.ready_to_materialize);
  captureResync(workId, ws);
  captureChapterStatus(workId, ws);
  const remote = { drafts: {}, scaffolds: {}, checks: {}, states: {}, _t: 0 };
  const health = {};
  let any = false;
  const canonMine = snowCanon[workId] || (snowCanon[workId] = {});
  (ws && ws.steps ? ws.steps : []).forEach(step => {
    const feKey = FE_BY_BE[step.step_key];
    if (!feKey) return;
    health[feKey] = shapeStepHealth(step);
    const draft = step.draft || {};
    canonMine[feKey] = stripFe(draft); // 服务端规范草稿镜像：先于 lastPushed 预填，保证 sig 一致
    if (draft.fe_scaffold || draft.fe_text || draft.fe_state) {
      any = true;
      if (draft.fe_text != null) remote.drafts[feKey] = draft.fe_text;
      if (draft.fe_scaffold) remote.scaffolds[feKey] = draft.fe_scaffold;
      if (Array.isArray(draft.fe_checks)) remote.checks[feKey] = draft.fe_checks;
      if (draft.fe_state) remote.states[feKey] = draft.fe_state;
      if (draft.fe_t && draft.fe_t > remote._t) remote._t = draft.fe_t;
      if (draft.fe_meta) {
        // E3 第二步：旧写穿里的 fe_meta.revs / confirmRevs 直接忽略——失效真相在后端
        // G2：跨会话 journal（去快照、cap 20）——视图只对带 snap 的条目给回滚按钮，
        // 还原条目天然只读，不需要视图改动
        if (Array.isArray(draft.fe_meta.history)) remote.history = draft.fe_meta.history;
      }
    } else if (canonHasContent(feKey, draft)) {
      any = true;
      const fe = feFromCanon(feKey, draft);
      if (fe.text != null) remote.drafts[feKey] = fe.text;
      if (fe.scaffold) remote.scaffolds[feKey] = fe.scaffold;
      const st = BE_STATE_TO_FE[step.status];
      remote.states[feKey] = st || (step.step_key === ws.current_step_key ? "active" : "todo");
      if (remote._t < 1) remote._t = 1; // 规范字段水合：极小时间戳，本地编辑永远赢
    }
  });
  snowHealth[workId] = health;
  try { window.dispatchEvent(new CustomEvent("ws:snow-health", { detail: workId })); } catch (e) {}
  if (!any) return; // 服务端还没有构思数据：保留本地（含种子门控默认）
  // BUG-2 防回退：用后端真相预填 lastPushed（去重账本），使随后第一个 autosave 不再把这些未改动的步骤
  // 全量 re-push。否则新会话 lastPushed 为空 → snowPushKey 全量上行：后端 update_step 对非 pending_review
  // 步走 else 分支新建 pending_review 版本，把「已确认」步静默打回待审，并产生无谓写/approve 噪声。
  // 注意：仅 seed 从后端水合到内容的步骤；本地新增、后端尚无的步骤不 seed，照常上行（不丢失）。
  let approvalRetryNeeded = false;
  try {
    const mine = lastPushed[workId] || (lastPushed[workId] = {});
    const hydratedKeys = new Set([
      ...Object.keys(remote.drafts), ...Object.keys(remote.states), ...Object.keys(remote.scaffolds),
    ]);
    hydratedKeys.forEach(feKey => {
      const frag = buildStepFragment(feKey, remote, workId);
      const approvalPending = frag.fe_state === "done"
        && ((health[feKey] || {}).beStatus === "pending_review");
      mine[feKey] = {
        sig: stepSig(frag),
        state: frag.fe_state,
        // 本机的 done 是“作者希望确认”，服务端 pending_review 才是权威未批状态。
        // 两者签名完全相同时也必须保留待批准账，否则新会话永远不会再发 approve。
        approvalPending,
      };
      approvalRetryNeeded = approvalRetryNeeded || approvalPending;
    });
  } catch (e) {}
  const key = snowCacheKey(workId);
  let local = null;
  try { local = JSON.parse(localStorage.getItem(key)); } catch (e) {}
  if (local && (local._t || 0) >= remote._t) {
    // 水合本身就发现“本机已确认、服务端仍待审”时主动补批，不再依赖视图恰好
    // 触发一次 autosave 或作者手点重试。尤其是十步草稿预先导入的场景，若只补第 1 步，
    // UI 会误报“服务器已同步”，真正的物化闸门却仍卡在第 2 步。
    if (approvalRetryNeeded) schedulePush(key);
    return; // 本地不旧于服务端：本地为准
  }
  try { localStorage.setItem(key, JSON.stringify(remote)); } catch (e) {}
  try { window.dispatchEvent(new CustomEvent("ws:snow-hydrated", { detail: workId })); } catch (e) {}
  if (approvalRetryNeeded) schedulePush(key);
}

/* ---------- 上行 ---------- */
let pushTimer = null;
let pendingKeys = new Set();
let pushChain = Promise.resolve();
const lastPushed = {}; // workId -> feKey -> { sig, state }

async function snowPushKey(cacheKey) {
  const workId = cacheKey.split("::")[1];
  if (!workId || snowUnsupported[workId]) return;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(cacheKey)); } catch (e) {
    setSnowSyncState(workId, { phase: "error", error: snowErrorShape(e, "无法读取本机构思缓存", "local") });
  }
  if (!saved) return readSnowSyncState(workId);
  const mine = lastPushed[workId] || (lastPushed[workId] = {});
  const work = SNOW_STEPS.filter(([feKey]) => {
    const fragment = buildStepFragment(feKey, saved, workId);
    const prev = mine[feKey] || {};
    return prev.sig !== stepSig(fragment) || prev.approvalPending === true;
  });
  if (!work.length) {
    setSnowSyncState(workId, { phase: "synced", pendingSteps: [], error: null, lastSyncedAt: Date.now() });
    return readSnowSyncState(workId);
  }
  setSnowSyncState(workId, { phase: "syncing", pendingSteps: work.map(([feKey]) => feKey), error: null });
  const failures = [];
  let pushedSceneish = false; // 9/10 步的改动会改变物化后的 resync_status
  for (const [feKey, beKey] of work) {
    const fragment = buildStepFragment(feKey, saved, workId);
    const sig = stepSig(fragment);
    const prev = mine[feKey] || {};
    if (prev.sig !== sig) {
      try {
        const patched = await apiPatch(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}`, { draft: fragment, force: true });
        const patchedStatus = patched && patched.step && patched.step.status;
        const approvalPending = fragment.fe_state === "done"
          && patchedStatus !== "approved"
          && patchedStatus !== "skipped";
        mine[feKey] = { sig, state: fragment.fe_state, approvalPending };
        if (feKey === "scenes" || feKey === "planning") pushedSceneish = true;
        // update_step 回包带最新 step.health/completeness → 增量刷新后端权威评估（无需再拉全量）
        if (patched && patched.step) {
          // 服务端把 draft 过了模板归一化（可能补齐空模板键）——刷新 canon 镜像并
          // 用新镜像重算 sig 记账，否则下轮 save 会因归一化差异多推一次空转 PATCH
          (snowCanon[workId] || (snowCanon[workId] = {}))[feKey] = stripFe(patched.step.draft || {});
          mine[feKey] = {
            sig: stepSig(buildStepFragment(feKey, saved, workId)),
            state: fragment.fe_state,
            approvalPending,
          };
          (snowHealth[workId] || (snowHealth[workId] = {}))[feKey] = shapeStepHealth(patched.step);
          window.dispatchEvent(new CustomEvent("ws:snow-health", { detail: workId }));
        }
      } catch (error) {
        if (error && error.status === 409 && error.code === "PROJECT_NOT_SNOWFLAKE") {
          snowUnsupported[workId] = true;
          const shaped = snowErrorShape(error, "当前作品未启用雪花工作台");
          setSnowSyncState(workId, { phase: "error", pendingSteps: [feKey], error: shaped });
          return readSnowSyncState(workId);
        }
        failures.push({ feKey, beKey, stage: "patch", error });
        continue; // PATCH 未成功，绝不能继续 approve
      }
    }

    // PATCH 回包的服务端状态优先：本机首次载入时已经是 done，也必须把 pending_review
    // 补批准；不能只依赖“本会话观察到 active → done”，否则离线/刷新后的完成态会永久卡住。
    // 批准失败会写 approvalPending，重试时即使 PATCH 已成功也会再次批准。
    const currentLedger = mine[feKey] || prev;
    const shouldApprove = fragment.fe_state === "done"
      && (currentLedger.approvalPending === true || (prev.state && prev.state !== "done"));
    if (!shouldApprove) continue;
    try {
      const appr = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/approve`, {});
      mine[feKey] = { ...(mine[feKey] || {}), state: "done", approvalPending: false };
      if (appr && appr.step) {
        (snowHealth[workId] || (snowHealth[workId] = {}))[feKey] = shapeStepHealth(appr.step);
        captureWorkspaceHealth(workId, appr.workspace); // 下游 stale 立即可见
        window.dispatchEvent(new CustomEvent("ws:snow-health", { detail: workId }));
      }
    } catch (error) {
      mine[feKey] = { ...(mine[feKey] || {}), state: "done", approvalPending: true };
      // 「需要先确认前面的雪花步骤」不是同步故障：本步 draft 已由上面的 PATCH 存到服务器，
      // 只是「确认」被上游依赖挡住。保留 approvalPending，等作者补确认前面步骤后，下一次
      // autosave 会按序自动补批；但绝不计入 failures——否则每次编辑都把这条正常的依赖等待
      // 谎报成红色「仅本机已保存 · 服务器同步失败」，且「重试」按钮对它毫无作用（重试不会
      // 替作者确认前面的步骤）。其余 approve 失败（真·闸门/网络）照旧上报并可重试。
      if (!(error && error.code === "SNOWFLAKE_PREVIOUS_STEP_REQUIRED")) {
        failures.push({ feKey, beKey, stage: "approve", error });
      }
    }
  }
  // 物化过（目录里已有章）的作品：9/10 步保存后强制重拉一次工作台，让「N 场待同步」
  // 的回流横幅跟上。hydrate 的 _t 比较保证较新的本地草稿不会被回写覆盖。
  if (pushedSceneish) {
    try {
      const hasCatalog = !!(WsCatalog && WsCatalog.get && WsCatalog.get().length);
      if (hasCatalog) await snowHydrate(workId, { force: true });
    } catch (e) {}
  }
  if (failures.length) {
    const first = failures[0];
    const stageLabel = first.stage === "approve" ? "后端批准" : "服务器保存";
    setSnowSyncState(workId, {
      phase: "error",
      pendingSteps: [...new Set(failures.map(item => item.feKey))],
      error: snowErrorShape(first.error, `${stageLabel}失败，本机版本已保留`),
      failures: failures.map(item => ({ feKey: item.feKey, beKey: item.beKey, stage: item.stage, code: item.error && (item.error.code || item.error.status) })),
    });
  } else {
    setSnowSyncState(workId, { phase: "synced", pendingSteps: [], failures: [], error: null, lastSyncedAt: Date.now() });
  }
  return readSnowSyncState(workId);
}

function schedulePush(cacheKey) {
  pendingKeys.add(cacheKey);
  const workId = String(cacheKey || "").split("::")[1];
  if (workId) setSnowSyncState(workId, { phase: "local_only", localSavedAt: Date.now(), error: null });
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => {
    const keys = [...pendingKeys];
    pendingKeys = new Set();
    keys.forEach(k => { pushChain = pushChain.then(() => snowPushKey(k)).catch(() => {}); });
  }, 700);
}

/* 分章预览/物化是雪花流程的下一跳，必须先排空 450ms 本机保存 + 700ms 上行防抖。
   否则作者刚点“确认本步”就点“整理”，预览会抢在 PATCH/approve 前读到旧闸门。 */
async function flushSnowPush(workId) {
  const id = workId || activeWork();
  if (!id) return readSnowSyncState(id);
  const key = snowCacheKey(id);
  // 先向仍挂载的雪花视图要一份“此刻内存态”的同步落盘，跨过视图自身 450ms 的
  // localStorage 防抖。事件是同步分发的；视图写完会立刻发 ws:snow-saved，把 key
  // 放进下面要排空的队列。作者页直达等没有雪花视图的场景则只排已有队列。
  try {
    window.dispatchEvent(new CustomEvent("ws:snow-flush-local", { detail: { workId: id } }));
  } catch (e) {}
  if (pendingKeys.has(key)) {
    pendingKeys.delete(key);
    if (!pendingKeys.size) {
      clearTimeout(pushTimer);
      pushTimer = null;
    }
    pushChain = pushChain.catch(() => {}).then(() => snowPushKey(key));
  }
  await pushChain.catch(() => {});
  return readSnowSyncState(id);
}

async function attachMaterializationGate(result, workId) {
  try {
    const workspace = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace`);
    snowReadyFlags[workId] = !!(workspace && workspace.ready_to_materialize);
    captureResync(workId, workspace);
    captureChapterStatus(workId, workspace);
    return { ...(result || {}), materialization_gate: (workspace && workspace.materialization_gate) || null };
  } catch (error) {
    // 预览本身已经成功时，不因第二次只读检查失败而抹掉方案；最终 materialize 仍会
    // 由后端权威闸门把关，并把最新 details 回给面板。
    return { ...(result || {}), materialization_gate: null };
  }
}

const previousGlobalHandlers = window.__snowSyncGlobalHandlers;
if (previousGlobalHandlers) {
  try { window.removeEventListener("ws:snow-saved", previousGlobalHandlers.saved); } catch (e) {}
  try { window.removeEventListener("hashchange", previousGlobalHandlers.hashchange); } catch (e) {}
  try { window.removeEventListener("ws:work-changed", previousGlobalHandlers.workChanged); } catch (e) {}
  try { clearTimeout(previousGlobalHandlers.hydrateTimer); } catch (e) {}
}

const onSnowSaved = (e) => {
  const key = (e && e.detail) || (activeWork() ? snowCacheKey(activeWork()) : null);
  if (key) schedulePush(key);
};

/* ---------- 触发面 ---------- */
const onSnowHashChange = () => {
  const h = location.hash || "";
  if (h.indexOf("snowflake") >= 0 || h.indexOf("home") >= 0) snowHydrate(activeWork());
};
const onSnowWorkChanged = (e) => { if (e && e.detail) snowHydrate(e.detail); };
window.addEventListener("ws:snow-saved", onSnowSaved);
window.addEventListener("hashchange", onSnowHashChange);
window.addEventListener("ws:work-changed", onSnowWorkChanged);
// 启动水合（等待 WsWorks 就绪）。句柄必须单独保存，便于 HMR/测试模块重载时撤销旧监听器。
const snowHydrateTimer = setTimeout(() => snowHydrate(activeWork()), 600);
window.__snowSyncGlobalHandlers = {
  saved: onSnowSaved,
  hashchange: onSnowHashChange,
  workChanged: onSnowWorkChanged,
  hydrateTimer: snowHydrateTimer,
};

const SnowSync = {
  refetch(workId) { return snowHydrate(workId || activeWork(), { force: true }); },
  syncState(workId) { return { ...readSnowSyncState(workId || activeWork()) }; },
  retry(workId) {
    const id = workId || activeWork();
    if (!id) return Promise.reject(new Error("作品尚未就绪"));
    const key = snowCacheKey(id);
    pushChain = pushChain.catch(() => {}).then(() => snowPushKey(key));
    return pushChain;
  },
  markLocalFailure(error, workId) {
    const id = workId || activeWork();
    setSnowSyncState(id, {
      phase: "error",
      error: snowErrorShape(error, "本机自动保存失败，请先导出构思", "local"),
      pendingSteps: SNOW_STEPS.map(([feKey]) => feKey),
    });
    return readSnowSyncState(id);
  },
  readyToMaterialize(workId) { return !!snowReadyFlags[workId || activeWork()]; },
  /* 结构化雪花计划导入：这是作者从既有策划稿/外部大纲迁入十步工作台的正常入口。
     UI 一次提交后仍逐步走现有 PATCH + approve 契约，依赖闸门、历史版本、场景身份铸造
     和审计日志均不绕过；任一步失败立即停止，不把半成品谎称为 10/10。 */
  async importCanonicalPlan(workId, payload) {
    const id = workId || activeWork();
    if (!id || id === "__loading__") throw new Error("请先选择一个作品再导入雪花计划。");
    const stepDrafts = payload && payload.steps && typeof payload.steps === "object" ? payload.steps : payload;
    if (!stepDrafts || typeof stepDrafts !== "object" || Array.isArray(stepDrafts)) {
      throw new Error("结构化计划必须是包含 steps 的 JSON 对象。");
    }
    const requiredKeys = SNOW_STEPS.map(([, beKey]) => beKey);
    const missing = requiredKeys.filter((key) => !stepDrafts[key] || typeof stepDrafts[key] !== "object" || Array.isArray(stepDrafts[key]));
    if (missing.length) throw new Error(`结构化计划缺少十步草稿：${missing.join("、")}`);

    const approvedStepKeys = [];
    const importedAt = Date.now();
    const local = {
      drafts: {}, scaffolds: {}, checks: {}, states: {},
      history: [{
        t: importedAt,
        who: "我",
        action: "导入结构化计划",
        note: "十步依赖顺序保存并由后端批准",
        key: "planning",
        snap: null,
      }],
      _t: importedAt,
    };
    for (const [feKey, beKey] of SNOW_STEPS) {
      const draft = stepDrafts[beKey];
      const patched = await apiPatch(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}`, { draft, force: true });
      const patchStep = patched && patched.step;
      if (patchStep) {
        (snowCanon[id] || (snowCanon[id] = {}))[feKey] = stripFe(patchStep.draft || draft);
        (snowHealth[id] || (snowHealth[id] = {}))[feKey] = shapeStepHealth(patchStep);
      }
      const approved = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}/approve`, {});
      const approvedStep = approved && approved.step;
      if (!approvedStep || approvedStep.status !== "approved") {
        throw new Error(`「${beKey}」未得到后端批准，导入已停止。`);
      }
      (snowCanon[id] || (snowCanon[id] = {}))[feKey] = stripFe(approvedStep.draft || draft);
      (snowHealth[id] || (snowHealth[id] = {}))[feKey] = shapeStepHealth(approvedStep);
      const fe = feFromCanon(feKey, approvedStep.draft || draft);
      if (fe && fe.text != null) local.drafts[feKey] = fe.text;
      if (fe && fe.scaffold) local.scaffolds[feKey] = fe.scaffold;
      local.states[feKey] = "done";
      approvedStepKeys.push(beKey);
    }

    const workspace = await apiGet(`/api/v2/projects/${id}/snowflake-workspace`);
    snowReadyFlags[id] = !!(workspace && workspace.ready_to_materialize);
    captureResync(id, workspace || {});
    captureChapterStatus(id, workspace || {});
    const normalizedLocal = s2NormalizeState(local);
    try { localStorage.setItem(snowCacheKey(id), JSON.stringify(normalizedLocal)); } catch (e) {}
    // The import already wrote and approved every step. Seed the autosave
    // dedupe ledger with that exact local snapshot, otherwise the history/local
    // state update emitted immediately after the modal closes schedules a
    // second ten-step PATCH wave which can race materialization back to 409.
    const mine = lastPushed[id] || (lastPushed[id] = {});
    for (const [feKey] of SNOW_STEPS) {
      const fragment = buildStepFragment(feKey, normalizedLocal, id);
      mine[feKey] = { sig: stepSig(fragment), state: fragment.fe_state };
    }
    try { window.dispatchEvent(new CustomEvent("ws:snow-health", { detail: id })); } catch (e) {}
    try { window.dispatchEvent(new CustomEvent("ws:snow-hydrated", { detail: id })); } catch (e) {}
    setSnowSyncState(id, { phase: "synced", pendingSteps: [], error: null, localSavedAt: importedAt, lastSyncedAt: Date.now() });
    return { approvedStepKeys, readyToMaterialize: snowReadyFlags[id], workspace };
  },
  /* 物化后回流状态：pending = 构思 9/10 步领先于目录场景卡的场（后端 resync_status 真相）。
     随 ws:snow-resync 事件更新（hydrate 全量 / 9-10 步保存后的强制重拉 / resync 回包）。 */
  resyncStatus(workId) { return snowResync[workId || activeWork()] || { pendingCount: 0, pendingScenes: [] }; },
  /* 把构思的改动写回目录场景卡（POST /resync：SceneCard 三拍/POV/题名 + 章 brief），
     成功后目录重拉，写作台 / AI 起草台即拿到最新场景卡。scenePlanIds 缺省 = 全部待同步场。
     返回 { synced, skipped, results, notice }；失败上抛由调用方诚实提示。
     notice = 后端「有一部分没能回流」的如实交代（目前只有一种：场要搬进的章还没被
     「整理为章节结构」写进目录，外键指不过去），调用方必须显示它 —— 只报 synced
     会让作者以为回流做完了，目录其实还停在上一版章节结构。 */
  async resync(workId, scenePlanIds) {
    const id = workId || activeWork();
    const body = Array.isArray(scenePlanIds) && scenePlanIds.length ? { scene_plan_ids: scenePlanIds } : {};
    const data = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/resync`, body);
    if (data && data.workspace) captureResync(id, data.workspace);
    try { if (WsCatalog && WsCatalog.__refresh) await WsCatalog.__refresh(id); } catch (e) {}
    const results = (data && data.results) || [];
    return {
      synced: results.filter(r => r && r.synced).length,
      skipped: results.filter(r => r && !r.synced).length,
      results,
      notice: (data && data.notice) || null,
    };
  },
  /* 后端 per-step 权威健康（feKey -> {score,status,gaps,nextActions,missingFields,gateSatisfied,...}）；
     视图用它显示「后端评估」区，与本地实时估算区分。随 ws:snow-health / ws:snow-hydrated 更新。 */
  health(workId) { return snowHealth[workId || activeWork()] || {}; },
  /* 阶段 E：「已复核」在服务端留痕——POST accept-stale，回包刷新权威健康。只对后端 status=stale 的步有意义。 */
  async acceptStale(workId, feKey, note) {
    const id = workId || activeWork();
    const beKey = BE_BY_FE[feKey];
    if (!id || !beKey) throw new Error("步骤未知，无法记录复核");
    const res = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}/accept-stale`, note ? { note } : {});
    if (res && res.step) {
      (snowHealth[id] || (snowHealth[id] = {}))[feKey] = shapeStepHealth(res.step);
      captureWorkspaceHealth(id, res.workspace); // 下游闸门随之变化
      try { window.dispatchEvent(new CustomEvent("ws:snow-health", { detail: id })); } catch (e) {}
    }
    return (snowHealth[id] || {})[feKey] || null;
  },
  /* 阶段 E：上游改了什么——按本步 artifact.input_refs（确认/写入时消费的上游 step_run_id）对照各上游
     现在的 step_run_id，变了的上游拉 history?include_draft=true，把消费版本与现在版本折成可读文本并排返回。
     没有记录（旧数据）或上游没变 → 空数组，视图据此提示。 */
  async upstreamChanges(workId, feKey) {
    const id = workId || activeWork();
    const health = snowHealth[id] || {};
    const refs = ((health[feKey] || {}).inputRefs) || {};
    const changed = Object.entries(refs).map(([beKey, oldRunId]) => {
      const upFe = FE_BY_BE[beKey];
      if (!upFe) return null;
      const now = (health[upFe] || {}).stepRunId || null;
      return (oldRunId && now && oldRunId !== now) ? { feKey: upFe, beKey, oldRunId, newRunId: now } : null;
    }).filter(Boolean);
    const items = [];
    for (const c of changed) {
      const hist = await apiGet(`/api/v2/projects/${id}/snowflake-workspace/steps/${c.beKey}/history?include_draft=true`);
      const rows = (hist && Array.isArray(hist.items)) ? hist.items : [];
      const oldRow = rows.find(r => r.step_run_id === c.oldRunId) || null;
      const newRow = rows.find(r => r.step_run_id === c.newRunId) || rows[0] || null;
      items.push({
        ...c,
        oldFound: !!oldRow,
        oldVersion: oldRow ? oldRow.version : null,
        newVersion: newRow ? newRow.version : null,
        oldText: oldRow ? canonText(c.feKey, oldRow.draft) : "",
        newText: newRow ? canonText(c.feKey, newRow.draft) : "",
      });
    }
    return items;
  },
  /* 「采纳并结构化」接缝（AI 融合 F1）：generate 回包的 step 落进本地——
     刷新 canon 镜像（后续 push 在它之上保真合并）与权威健康，并把规范草稿
     反推成原型形状 {text?, scaffold?} 交视图写入 drafts/scaffolds。 */
  applyServerStep(workId, feKey, step) {
    const id = workId || activeWork();
    if (!id || !feKey || !step) return null;
    const canon = stripFe(step.draft || {});
    (snowCanon[id] || (snowCanon[id] = {}))[feKey] = canon;
    (snowHealth[id] || (snowHealth[id] = {}))[feKey] = shapeStepHealth(step);
    try { window.dispatchEvent(new CustomEvent("ws:snow-health", { detail: id })); } catch (e) {}
    return feFromCanon(feKey, canon);
  },
  /* 视图把当前内存态折成本步规范草稿（如场景分诊的 draft_override）——
     避免「刚编辑还没自动保存上行」的竞态；不含 fe_* 写穿键。 */
  canonDraft(feKey, cache) { return canonFromFE(feKey, cache || {}); },
  /* structuredGenerate 的 draft_override：与上行 PATCH 完全同源的规范草稿
     （服务端 canon 镜像 ⊕ 当前脚手架，数组按 id 对位、FE 成员为准）——
     generate 的底稿据此看到「刚加的角色/场还没自动保存上行」的内容，
     且成员 id（character_id/scene_id）齐全，服务端能按 id 对位合并。 */
  pushCanon(feKey, cache, workId) {
    const id = workId || activeWork();
    const canon = canonFromFE(feKey, cache || {});
    const server = id ? ((snowCanon[id] || {})[feKey] || null) : null;
    return server ? mergeCanon(server, canon) : canon;
  },
  /* 教练 candidate_patch 落地：以「服务端 canon 镜像 ⊕ 当前脚手架」为底，
     咨询式合并补丁（空值不清空、按 id 对位、不删成员），反推回原型形状。 */
  applyCanonPatch(feKey, cache, patch, workId) {
    const id = workId || activeWork();
    const canon = canonFromFE(feKey, cache || {});
    const server = id ? ((snowCanon[id] || {})[feKey] || null) : null;
    const base = server ? mergeCanon(server, canon) : canon;
    return feFromCanon(feKey, applyCanonPatch(base, patch || {}));
  },
  /* 分章现状（后端只读真相）：{chapter_count, unassigned_scene_count, chaptered, …}。
     顶部「整理为章节结构」据此决定是直接开面板还是先提示补 07 章表。 */
  chapterPlanStatus(workId) { return snowChapterStatus[workId || activeWork()] || { chapter_count: 0, unassigned_scene_count: 0, chaptered: false }; },
  /* 分章预览：只读推演，不落库。strategy = spine_anchor（默认，脊柱锚点）/ even / keep_current。 */
  async chapterPreview(strategy, workId) {
    const id = workId || activeWork();
    await flushSnowPush(id);
    const preview = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/chapter-plan/preview`, strategy ? { strategy } : {});
    return attachMaterializationGate(preview, id);
  },
  /* 让 AI 给一份分章建议（只读，不落库）。fail-closed：LLM 没配好会 409 上抛，
     调用方如实提示去配置 —— 绝不拿规则算出来的东西冒充 AI 建议。 */
  async chapterSuggest(baseStrategy, workId) {
    const id = workId || activeWork();
    await flushSnowPush(id);
    const suggestion = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/chapter-plan/suggest`,
      baseStrategy ? { base_strategy: baseStrategy } : {});
    return attachMaterializationGate(suggestion, id);
  },
  /* 处置孤儿场：action = "discard"（正文一并进回收站）/ "keep"（正文留在目录里）。
     孤儿场 = 作者从 09 删掉、但目录里已经有场景卡（可能已写正文）的那些场。它们是
     分章面板上的 blocker，没有这个动作就永远清不掉，「确认分章」按钮从此点不动。 */
  async resolveOrphanedScene(scenePlanId, action, workId) {
    const id = workId || activeWork();
    const data = await apiPost(
      `/api/v2/projects/${id}/snowflake-workspace/orphaned-scenes/${scenePlanId}/resolve`,
      { action });
    if (data && data.workspace) captureChapterStatus(id, data.workspace);
    return data;
  },
  /* 只保存分章（不物化）：作者在面板里调完想先存一版。 */
  async saveChapterPlan(payload, workId) {
    const id = workId || activeWork();
    const data = await apiPatch(`/api/v2/projects/${id}/snowflake-workspace/chapter-plan`, payload || {});
    if (data && data.workspace) captureChapterStatus(id, data.workspace);
    return data;
  },
  /* 物化主路径：approved scene plans → ChapterGoal/SceneCard（成功后目录重拉）。
     plan = 分章面板确认时的 {chapters, assignments}，与物化同一事务落库，
     不留「分了章但没物化」的中间态。
     注意：materialize 端点只建 pending OutlinePlan，章节要 outline/approve 才落库——
     必须两步都走，否则目录为空却谎称「已并入 N 章」。返回真实 created_chapter_count。 */
  async materialize(workId, plan) {
    const id = workId || activeWork();
    await flushSnowPush(id);
    const data = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/materialize`, plan || {});
    const approved = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/outline/approve`, {});
    try { if (WsCatalog && WsCatalog.reset) WsCatalog.reset(); } catch (e) {}
    const createdChapters = (approved && approved.created_chapter_count) || 0;
    return { ...(data || {}), created_chapter_count: createdChapters };
  },
};

Object.assign(window, { SnowSync });

// mergeCanon / applyCanonPatch / feFromCanon / canonFromFE 一并导出：供 store 单测
// 直接验证「保真合并」「咨询式补丁」与「规范字段 ↔ 原型形状」的往返契约
export { SnowSync, mergeCanon, applyCanonPatch, feFromCanon, canonFromFE };
