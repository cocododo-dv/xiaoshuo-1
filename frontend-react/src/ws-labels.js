/* ==========================================================
   ws-labels — 章 / 场的叫法与章节状态词表（2026-09-21 前端重构）
   ----------------------------------------------------------
   审计时同一章在四个视图里有四种写法：「第 01 章」「第1章 · x」、后端 id
   PRJ_…_CH01、成本看板拿前端 slug 去比后端 id 永远对不上；章节状态在成稿中心叫
   「计划中 / 待聚合」、在主页叫「规划」。这里给一份：
   · CHAPTER_STATE_META —— 目录状态 → 中文叫法 / 语气色（ws-ui 的 tone）；SCENE_STATE_META —— 场的三态；
   · manuscriptStage —— 成稿中心看的是稿子走到哪一步，不是目录上手打的标签；
   · chapterLabel / sceneLabel / *ById —— 后端 id → 「第 N 章 · 章名」「第 N 章 · 第 M 场」；
   · 待办来源、写作偏好、模型记账状态、模型节点的中文名；
   · 风格参考的一张词表：16 维（与后端 card.DIMENSION_LABELS 逐字相同）、段落类型、样例窗口在章里的位置、
     场面 / 情绪标签（与后端 tags.py 同一套词）、参考书作业的叫法。
   纯函数：章节列表由调用方传入（通常是 WsCatalog.get()），不读 store、不写 window。
   ========================================================== */

/* ---------- 章节状态 ---------- */

/* 与后端 CatalogService.CHAPTER_STATES 同源；数组顺序即图例顺序。
   tone 用 ws-ui 的语气：ok 完成、warn 等你拍板、info 中性、accent 正在进行。
   一个状态只有一个叫法：以前还有一列「短称」（定稿 / 送审 / 在写 / 规划），主页图例用短称、悬停说明用全称，
   同一章在主页叫「在写」、在成稿中心和章节编排叫「写作中」。 */
export const CHAPTER_STATE_ORDER = ["approved", "review", "draft", "writing", "planned", "todo"];

export const CHAPTER_STATE_META = {
  approved: { label: "已定稿", tone: "ok" },
  review: { label: "审阅中", tone: "warn" },
  draft: { label: "草稿", tone: "info" },
  writing: { label: "写作中", tone: "accent" },
  planned: { label: "规划中", tone: "neutral" },
  todo: { label: "待写", tone: "neutral" },
};

/* 认不出的状态（旧数据、别名 plan）一律按「规划中」显示——视图不会因为一个新状态整页崩掉。 */
export function chapterStateMeta(state) {
  if (state === "plan") return CHAPTER_STATE_META.planned;
  return CHAPTER_STATE_META[state] || CHAPTER_STATE_META.planned;
}

function hasManuscriptText(chapter) {
  if (!chapter) return false;
  if (Number(chapter.words && chapter.words.cur) > 0) return true;
  return (chapter.scenes || []).some((scene) => scene && (scene.state === "done" || scene.state === "archived"));
}

/* 成稿中心的阶段：已定稿 / 审阅中 / 草稿照目录；其余（写作中、规划、待写）只要已经有字
   或有写完的场，就是「写作中」——一章写了四千字还挂着「计划中」，作者读不懂。 */
export function manuscriptStage(chapter) {
  const state = chapter && chapter.state;
  if (state === "approved" || state === "review" || state === "draft" || state === "writing") return state;
  if (hasManuscriptText(chapter)) return "writing";
  return state === "todo" ? "todo" : "planned";
}

/* ---------- 场景状态 ---------- */

/* 目录里一场写到哪儿（WsCatalog 的 scene.state：done / writing / todo）。一个状态一个词：
   以前同一场写完了，在章节编排叫「已完」、写作台大纲和 AI 起草台叫「已完成」、成稿中心列表叫「写完」、
   主页叫「完成」，成稿中心页头又叫「已归档」。写作台大纲把 writing 叫 active，起草台的运行记录把
   写完归档的场叫 archived——都按同一个词显示。 */
export const SCENE_STATE_ORDER = ["done", "writing", "todo"];

export const SCENE_STATE_META = {
  done: { label: "已完成", tone: "ok" },
  writing: { label: "写作中", tone: "accent" },
  todo: { label: "待写", tone: "neutral" },
};

export function sceneStateMeta(state) {
  if (state === "active") return SCENE_STATE_META.writing;
  if (state === "archived") return SCENE_STATE_META.done;
  return SCENE_STATE_META[state] || SCENE_STATE_META.todo;
}

/* ---------- 章 / 场的叫法 ---------- */

function chapterNumber(chapter) {
  const n = Number(chapter && chapter.n);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/* 系统起的占位章名（「第 3 章」「第三章」、手建章的「未命名 / 未命名章节」、07 章表里的「（待补）」）
   不再重复拼在编号后面。空串不算占位——调用方自己判断「没有章名」。 */
export function isPlaceholderChapterTitle(title) {
  const text = String(title || "").trim();
  return /^第\s*[0-9一二三四五六七八九十百千零〇]+\s*章$/.test(text) || /^(未命名(章节)?|（待补）)$/.test(text);
}

/* 章自己的名字；占位或没有时为空串——列表行只在它不空时才把「第 N 章」放在名字旁边。 */
export function chapterOwnTitle(chapter) {
  const title = String((chapter && chapter.title) || "").trim();
  return title && !isPlaceholderChapterTitle(title) ? title : "";
}

/* 列表行 / 卡头的两段：num 永远是「第 N 章」；title 是章自己的名字（占位时为空）。
   渲染约定：title 不空时 num 作小字放在名字旁；title 为空时只把 num 放在名字的位置——编号不和它自己的占位名并排。 */
export function chapterHeading(chapter) {
  return { num: chapterLabel(chapter, { withTitle: false }), title: chapterOwnTitle(chapter) };
}

/* 章内第几场：「第 3 场」（章已经由分组标题给出时用；index 从 0 起）。 */
export function sceneNoLabel(index) {
  const i = Number(index);
  return Number.isFinite(i) && i >= 0 ? `第 ${i + 1} 场` : "";
}

function clip(text, max) {
  const value = String(text || "").trim();
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

/* 「第 3 章 · 盐场」；没有章名或章名就是占位编号时只给「第 3 章」。 */
export function chapterLabel(chapter, { withTitle = true, maxTitle = 24 } = {}) {
  if (!chapter) return "";
  const n = chapterNumber(chapter);
  const head = n ? `第 ${n} 章` : "未编号的章";
  const title = String(chapter.title || "").trim();
  if (!withTitle || !title || isPlaceholderChapterTitle(title)) return head;
  return `${head} · ${clip(title, maxTitle)}`;
}

/* 后端 chapter_id（目录卡的 backendId）→ 目录章。 */
export function findChapterByBackendId(chapters, backendId) {
  if (!backendId || !Array.isArray(chapters)) return null;
  return chapters.find((chapter) => chapter && chapter.backendId === backendId) || null;
}

export function chapterLabelById(chapters, backendId, { fallback = "未关联章节", withTitle = true } = {}) {
  if (!backendId) return fallback;
  const chapter = findChapterByBackendId(chapters, backendId);
  return chapter ? chapterLabel(chapter, { withTitle }) : "已不在目录里的章";
}

/* 后端 scene_id → { chapter, scene, index }（index 从 0 起，是章内位置）。 */
export function findSceneByBackendId(chapters, sceneId) {
  if (!sceneId || !Array.isArray(chapters)) return null;
  for (const chapter of chapters) {
    const scenes = (chapter && chapter.scenes) || [];
    const index = scenes.findIndex((scene) => scene && scene.backendId === sceneId);
    if (index >= 0) return { chapter, scene: scenes[index], index };
  }
  return null;
}

/* 「第 3 章 · 第 2 场」；withTitle 时在后面补场题（截短），场题单独成段，不再多一个「·」。 */
export function sceneLabel(chapter, index, scene, { withTitle = false, maxTitle = 18 } = {}) {
  const n = chapterNumber(chapter);
  const no = sceneNoLabel(index);
  const head = `${n ? `第 ${n} 章` : "未编号的章"}${no ? ` · ${no}` : ""}`;
  const title = String((scene && scene.title) || "").trim();
  return withTitle && title ? `${head}「${clip(title, maxTitle)}」` : head;
}

export function sceneLabelById(chapters, sceneId, { fallback = "未关联场景", withTitle = false } = {}) {
  if (!sceneId) return fallback;
  const hit = findSceneByBackendId(chapters, sceneId);
  return hit ? sceneLabel(hit.chapter, hit.index, hit.scene, { withTitle }) : "已不在目录里的场";
}

/* ---------- 待办 ---------- */

/* 待办卡的 source 是后端记下的来源；旧表行直接用 item_type（英文键）。 */
export const REVIEW_SOURCE_LABELS = {
  author_preference_profile: "写作偏好",
  fe_card: "工作台",
};

export function reviewSourceLabel(source) {
  const key = String(source || "").trim();
  if (!key) return "";
  if (REVIEW_SOURCE_LABELS[key]) return REVIEW_SOURCE_LABELS[key];
  // 认不出的英文键不直接甩给作者
  return /^[a-z0-9_]+$/.test(key) ? "系统" : key;
}

/* 系统从作者手改 / 驳回提案里记下的写作倾向（后端 author_drafts 的 safe_preference_hints）。 */
export const PREFERENCE_HINT_LABELS = {
  prefer_expansion: "偏好扩写",
  prefer_concise: "偏好精简",
  prefer_more_dialogue: "偏好多写对白",
  prefer_less_dialogue: "偏好少写对白",
  prefer_longer_paragraphs: "偏好更长的段落",
  prefer_shorter_paragraphs: "偏好更短的段落",
  prefer_longer_sentences: "偏好更长的句子",
  prefer_shorter_sentences: "偏好更短的句子",
  prefer_voice: "看重人物的声音",
  prefer_structure: "看重结构与铺垫回收",
  avoid_exposition: "少交代、少解释",
  avoid_dialogue_style: "不喜欢那种对白写法",
  avoid_tone: "不喜欢那种腔调",
  avoid_pacing: "在意节奏",
  other_safe_note: "其他备注",
};

export function preferenceHintLabel(key) {
  return PREFERENCE_HINT_LABELS[key] || "";
}

/* ---------- 成本看板 ---------- */

export const ACCOUNTING_STATUS_LABELS = {
  settled: { label: "已结算", tone: "neutral" },
  reserved: { label: "预留中", tone: "info" },
  failed: { label: "失败", tone: "danger" },
  released: { label: "已释放", tone: "neutral" },
  rejected: { label: "发出前被拦下", tone: "warn" },
  usage_exceeds_reservation: { label: "用量超出预留", tone: "warn" },
};

export function accountingStatusMeta(status) {
  return ACCOUNTING_STATUS_LABELS[status] || { label: status ? "其他" : "—", tone: "neutral" };
}

/* 模型节点（后端 llm_node_registry 的 node_id）的中文名。认不出的节点返回空串，
   调用方回落到等宽显示原始 id（机器标识，放在 title 里也行）。 */
export const LLM_NODE_LABELS = {
  extraction: "通用抽取",
  snowflake_step_candidates: "构思 · 方向候选",
  snowflake_step_generate: "构思 · 生成本步",
  snowflake_workspace_assistant: "构思 · 教练",
  snowflake_scene_triage: "构思 · 场景分诊",
  snowflake_chapter_plan: "构思 · 分章建议",
  style_ref_paragraph_classify_anchor: "参考书 · 锚定段落分类",
  style_ref_paragraph_classify_bulk: "参考书 · 段落分类",
  style_ref_extract_language: "参考书 · 语言层抽取",
  style_ref_extract_narrative: "参考书 · 叙事层抽取",
  style_ref_extract_scene: "参考书 · 场景层抽取",
  style_ref_extract_theme: "参考书 · 主题层抽取",
  style_ref_synthesize_profile: "参考书 · 写文风卡",
  style_ref_protected_terms: "参考书 · 识别本书专名",
  style_ref_tag_windows: "参考书 · 给片段打标签",
  scene_blueprint: "场景蓝图",
  character_pressure_blueprint: "人物压力蓝图",
  chapter_story_architecture: "章节故事架构",
  chapter_scene_plan_candidates: "章节场景候选",
  chapter_scene_plan_fill: "章节场景补全",
  chapter_plan_review: "章节规划评审",
  neutral_draft: "初稿",
  style_draft: "风格稿",
  style_patch: "风格修补",
  scene_literary_rewrite: "文学改写",
  hard_qc: "硬质检",
  soft_qc: "软质检",
  near_final_acceptance_review: "准定稿评审",
  chapter_near_final_review: "章节准定稿评审",
  writer_passage_patch: "写作台 · 段落修补",
  writer_deep_review: "写作台 · 深度审读",
  author_proposal_generate: "写作台 · 修改提案",
  chapter_summary: "章节摘要",
  continuity_compression: "连续性压缩",
  archive: "归档与索引",
  chapter_aggregate: "章节汇总",
};

export function llmNodeLabel(nodeId) {
  return LLM_NODE_LABELS[nodeId] || "";
}

/* ---------- 风格参考（参考书）：一张词表 ----------
   16 维的名字与后端 services/style_reference/card.py 的 DIMENSION_LABELS 逐字相同（ws-labels.test.js 读后端源码
   比对）；场面 / 情绪标签与后端 tags.py 的 SITUATION_TAGS / MOOD_TAGS 同一套词。风格参考的各页、起草台与成稿中心
   说到这些词都从这里取，不再各写一份。 */

export const STYLE_LAYER_ORDER = ["language", "narrative", "scene", "theme"];

export const STYLE_LAYER_LABELS = {
  language: "语言",
  narrative: "叙事",
  scene: "场景",
  theme: "主题",
};

export const STYLE_DIMENSION_LABELS = {
  "language.sentence_structure": "句式结构",
  "language.vocabulary": "词汇选择",
  "language.rhetoric": "修辞手法",
  "language.punctuation": "标点节奏",
  "narrative.perspective": "叙事视角",
  "narrative.pacing": "节奏控制",
  "narrative.time_handling": "时间处理",
  "narrative.information_density": "信息密度",
  "scene.environment": "环境描写",
  "scene.character_portrayal": "人物刻画",
  "scene.dialogue": "对话写法",
  "scene.sensory_priority": "感官优先",
  "theme.emotional_tone": "情感基调",
  "theme.values": "价值取向",
  "theme.motifs": "母题意象",
  "theme.narrative_philosophy": "叙事哲学",
};

/* 16 维的固定顺序（语言 → 叙事 → 场景 → 主题，每层 4 维；与后端 SubDimension 同序） */
export const STYLE_DIMENSIONS = Object.keys(STYLE_DIMENSION_LABELS);

export function styleDimensionLabel(dimension) {
  return STYLE_DIMENSION_LABELS[dimension] || String(dimension || "");
}

export function styleLayerOf(dimension) {
  const layer = String(dimension || "").split(".", 1)[0];
  return STYLE_LAYER_LABELS[layer] ? layer : "";
}

/* 段落类型（分类作业给每一段标的类型）；unclassified 是分类作业还没轮到的段。 */
export const PARAGRAPH_TYPE_LABELS = {
  narration: "叙述",
  dialogue: "对话",
  description_env: "环境",
  psychology: "心理",
  action: "动作",
  description_char: "人物",
  transition: "转场",
  flashback: "闪回",
  unclassified: "未分类",
};

export function paragraphTypeLabel(type) {
  return PARAGRAPH_TYPE_LABELS[type] || String(type || "");
}

/* 样例窗口 / 场景在章里的位置 */
export const WINDOW_POSITION_LABELS = {
  opening: "章首",
  closing: "章末",
  whole: "整章",
  middle: "章中",
};

export function windowPositionLabel(position) {
  return WINDOW_POSITION_LABELS[position] || "";
}

/* 一场的样例窗是按哪条配额选进来的（后端 inject/selection 的 slot；本场预览与起草台「本场参考窗口」共用）。
   2026-09-24 O1：窗口标签不再有「手法」，改记这一窗最能示范的维度（16 维的键），配额随之叫「维度示范」；
   O1 之前冻结的场（style_reference_scene_windows 旧行）还带旧的 slot 名，照旧给个叫法。 */
export const STYLE_WINDOW_SLOT_LABELS = {
  position: "按章内位置挑",
  situation: "按场面挑",
  dimension: "维度示范",
  texture: "按对白 / 叙述的质地挑",
  typical: "全书典型片段",
  revise_dimension: "示范要改的维度",
  device: "维度示范",
  revise_device: "示范要改的维度",
};

export function styleWindowSlotLabel(slot) {
  return STYLE_WINDOW_SLOT_LABELS[slot] || "";
}

/* 场面 / 情绪标签（学习作业给全书窗口打的、场景蓝图给一场标的，同一套词） */
export const STYLE_SITUATION_TAGS = [
  "日常闲谈", "对峙审问", "争吵冲突", "打斗追逐", "危机应对", "独处内省", "回忆往事", "说明设定",
  "群像场面", "情感交流", "喜剧桥段", "悬疑揭示", "赶路转场", "计划商议", "开章引入", "收章落点",
];
export const STYLE_MOOD_TAGS = ["紧张", "诙谐", "伤感", "温情", "压抑", "热血", "荒诞", "悬疑", "平静", "恐惧"];

/* 参考书的耗时作业（作业表的 kind） */
export const STYLE_JOB_KIND_LABELS = {
  classify: "段落分类",
  learn: "学习文风",
  check: "对照检查",
};

export function styleJobKindLabel(kind) {
  return STYLE_JOB_KIND_LABELS[kind] || "";
}
