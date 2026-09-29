/* ==========================================================
   目录 · 后端载荷 ↔ 视图形状（纯函数；2026-09-29 从 ws-catalog.jsx 拆出）
   章 / 场从 /api/v2/projects/{id}/catalog 的形状映射成视图用的形状（设计卡、结构归属、真实工作状态），
   以及视图改动 → PATCH / POST 载荷。不读缓存、不发请求。
   ========================================================== */

/* 三拍的名字全站只有一套（构思第 10 步、设计卡、主页、章节编排同读）：主动 目标/冲突/挫败，反应 反应/两难/决定 */
export const KIND_FIELDS_GCS = ["目标", "冲突", "挫败"];
export const KIND_FIELDS_RDD = ["反应", "两难", "决定"];

/* 阶段 X：整张设计卡（坩埚 / 地点 / 时间 / 出场 / 读者情绪 / 必须包含·隐瞒 / 代价 / 篇幅带 / 呈现方式 /
   后续三拍 / 破例理由）随目录到达台子。后端没给（旧后端、测试夹具）时是一张空卡，视图照常渲染。 */
export function catDesignFromApi(s) {
  const d = (s && s.design) || {};
  const followup = d.followup || {};
  return {
    origin: d.origin === "snowflake" ? "snowflake" : "manual",
    crucible: d.crucible || "",
    location: d.location || "",
    storyTime: d.story_time || "",
    cast: Array.isArray(d.cast) ? d.cast.map(c => ({ id: c.character_id || "", name: c.name || c.character_id || "" })).filter(c => c.name) : [],
    readerEmotion: d.reader_emotion || "",
    mustInclude: d.must_include || "",
    mustWithhold: d.must_withhold || "",
    cost: d.cost || "",
    lengthBand: d.length_band || "",
    renderingMode: d.rendering_mode || "full",
    followup: {
      goal: followup.goal || "", conflict: followup.conflict || "", setback: followup.setback || "",
      reaction: followup.reaction || "", dilemma: followup.dilemma || "", decision: followup.decision || "",
    },
    exceptionReason: d.exception_reason || "",
    protagonist: d.protagonist || "",
    chapterLast: !!d.is_chapter_last,
    /* 阶段 Y「设计只有一处可改」：owner = "plan" 的场（雪花整理出来、构思里那一行还在）设计只在构思第 10 步改，
       台子上只读；"desk" = 就在章节编排里改。旧载荷没有 owner 时按来源猜。 */
    owner: (d.owner === "plan" || (d.owner == null && d.origin === "snowflake")) ? "plan" : "desk",
    /* 阶段 Z：它在构思里是第几场（故事序，1 起；不在构思里的场为 0）——与分章面板、09 场景列表同一套编号 */
    storyIndex: Number(d.story_index) || 0,
  };
}

/* 阶段 Z「一张章表、两扇门」：这一章的结构归谁改。owner = "plan" 的章（构思的分章钉着它）彼此的先后与幕
   只在「整理章节结构」里改；章名两边改的是同一个（后端写穿到章计划）。sceneRange = 它装着故事序上第几到第几场。 */
export function catStructureFromApi(c) {
  const st = (c && c.structure) || {};
  const span = st.scene_range && Number(st.scene_range.first) > 0
    ? { first: Number(st.scene_range.first), last: Number(st.scene_range.last) || Number(st.scene_range.first) }
    : null;
  return {
    owner: st.owner === "plan" ? "plan" : "desk",
    rowUid: st.row_uid || "",
    sceneRange: span,
    plannedSceneCount: Number(st.planned_scene_count) || 0,
    titleAuto: !!st.title_auto,
  };
}

export function catFromApiScene(s) {
  const reactive = s.kind === "reactive";
  const b = s.brief || {};
  const work = s.work || {};
  return {
    /* 场景 sid = 后端给的 slug。阶段 X 起它就是稳定的 scene_id（身份跟着行走，不跟着位置走）；
       位置式旧 slug（ch08s3）只留作 legacySid，给旧深链兜底、给本机旧键做一次性迁移。 */
    sid: s.slug,
    legacySid: s.legacy_slug || "",
    backendId: s.scene_id,
    title: s.title,
    summary: s.summary || "",
    kind: reactive ? "反应" : "主动",
    state: s.state,
    words: s.words || 0,
    goal: reactive ? (b.reaction || "") : (b.goal || ""),
    obstacle: reactive ? (b.dilemma || "") : (b.conflict || ""),
    turn: reactive ? (b.decision || "") : (b.setback || ""),
    povName: s.pov_character_name || "",
    povId: s.pov_character_id || "",
    kindFields: reactive ? KIND_FIELDS_RDD : KIND_FIELDS_GCS,
    exitChange: s.exit_change || "",
    hook: s.hook || "",
    // 阶段 D：最近一次准定稿评审的场景三问（坩埚可辨 / 三拍落地 / Yes-No-Maybe），无评审则 null
    storyCheck: s.story_check || null,
    design: catDesignFromApi(s),
    // 真实的工作状态（目录 state 只是作者手打的标签）：管线状态 / 有无定稿 / 有无正文
    work: { runStatus: work.run_status || "", hasFinal: !!work.has_final, hasWords: !!work.has_words },
  };
}

/* 目录侧的幕只有 act1 / act2 / act3。后端读取时已归一；这里再守一道——章节编排按 act === "act1" 分卷，
   认不出的值会让一章从看板上整个消失（雪花物化曾把幕写成整数 1 / 2 / 3，正是这么消失的）。 */
export function catNormalizeAct(value) {
  const text = String(value == null ? "" : value).trim().toLowerCase();
  if (text === "act1" || text === "act2" || text === "act3") return text;
  const digit = /[123]/.exec(text);
  return digit ? "act" + digit[0] : "act1";
}

export function catFromApiChapter(c) {
  return {
    id: c.slug,
    backendId: c.chapter_id,
    act: catNormalizeAct(c.act),
    n: c.no,
    title: c.title,
    // 阶段 X：章从哪来（雪花整理 / 手建）、构思里给它写的章摘要 / 章目标 / 脊柱标记
    origin: c.origin === "snowflake" ? "snowflake" : "manual",
    summary: c.summary || "",
    goal: c.goal || "",
    spine: c.spine || "",
    structure: catStructureFromApi(c),
    state: c.state,
    tension: typeof c.tension === "number" ? c.tension : 0.3,
    /* 张力没有任何编辑入口（旧数据 / 夹具才有）：没设过就别让镜头和体检拿 0.3 的默认值当事实 */
    tensionSet: typeof c.tension === "number",
    pov: c.pov || "",
    time: c.time_label || "",
    place: c.place || "",
    current: !!c.current,
    words: { cur: (c.words && c.words.cur) || 0, target: (c.words && c.words.target) || 0 },
    entry: c.entry || "",
    exit: c.exit || "",
    align: c.align !== false,
    promise: c.promise || "",
    drama: { promise: "", spine: "", arc: "", problem: "", aftertaste: "", ending: "", forbidden: "", notes: "", ...(c.drama || {}) },
    threads: c.threads || [],
    scenes: (c.scenes || []).map(catFromApiScene),
  };
}

/* 章对象 diff → PATCH 载荷（只含变化字段） */
export function catChapterPatch(prev, next) {
  const patch = {};
  if (next.title !== prev.title) patch.title = next.title;
  if (next.state !== prev.state) patch.state = next.state;
  const prevTarget = (prev.words && prev.words.target) || 0;
  const nextTarget = (next.words && next.words.target) || 0;
  if (nextTarget !== prevTarget) patch.words_target = nextTarget || null;
  if (next.act !== prev.act) patch.act = next.act;
  if (next.tension !== prev.tension) patch.tension = next.tension;
  if (next.pov !== prev.pov) patch.pov = next.pov;
  if (next.time !== prev.time) patch.time_label = next.time;
  if (next.place !== prev.place) patch.place = next.place;
  if (next.entry !== prev.entry) patch.entry = next.entry;
  if (next.exit !== prev.exit) patch.exit = next.exit;
  if (next.align !== prev.align) patch.align = next.align;
  if (next.promise !== prev.promise) patch.promise = next.promise;
  if (JSON.stringify(next.drama || {}) !== JSON.stringify(prev.drama || {})) patch.drama = next.drama || {};
  if (JSON.stringify(next.threads || []) !== JSON.stringify(prev.threads || [])) patch.threads = next.threads || [];
  if (next.current && !prev.current) patch.current = true;
  return patch;
}

export function catScenePatch(prev, next) {
  const patch = {};
  if (next.title !== prev.title) patch.title = next.title;
  if (next.state !== prev.state) patch.state = next.state;
  // POV：FE 只跟「名字」打交道，后端按名 find-or-create 角色并回填 id（放在换型早退之前，避免同时改型丢 pov）
  if ((next.povName || "") !== (prev.povName || "")) patch.pov_character_name = next.povName || "";
  const reactive = next.kind === "反应";
  if (next.kind !== prev.kind) {
    patch.kind = reactive ? "reactive" : "proactive";
    // 换型时把三个槽位整体写到新键
    patch.brief = reactive
      ? { reaction: next.goal || "", dilemma: next.obstacle || "", decision: next.turn || "" }
      : { goal: next.goal || "", conflict: next.obstacle || "", setback: next.turn || "" };
    return patch;
  }
  const brief = {};
  if (next.goal !== prev.goal) brief[reactive ? "reaction" : "goal"] = next.goal || "";
  if (next.obstacle !== prev.obstacle) brief[reactive ? "dilemma" : "conflict"] = next.obstacle || "";
  if (next.turn !== prev.turn) brief[reactive ? "decision" : "setback"] = next.turn || "";
  if (Object.keys(brief).length) patch.brief = brief;
  return patch;
}

export function catSceneCreateBody(s, at) {
  const reactive = s.kind === "反应";
  return {
    title: s.title,
    kind: reactive ? "reactive" : "proactive",
    at,
    state: s.state === "active" ? "writing" : (s.state || "todo"),
    brief: reactive
      ? { reaction: s.goal || "", dilemma: s.obstacle || "", decision: s.turn || "" }
      : { goal: s.goal || "", conflict: s.obstacle || "", setback: s.turn || "" },
  };
}
