/* ==========================================================
   labels/catalog — 章 / 场的叫法与章节状态词表（2026-09-21 前端重构；2026-09-29 从 ws-labels.js 拆出）
   ----------------------------------------------------------
   审计时同一章在四个视图里有四种写法：「第 01 章」「第1章 · x」、后端 id
   PRJ_…_CH01、成本看板拿前端 slug 去比后端 id 永远对不上；章节状态在成稿中心叫
   「计划中 / 待聚合」、在主页叫「规划」。这里给一份：
   · CHAPTER_STATE_META —— 目录状态 → 中文叫法 / 语气色（ws-ui 的 tone）；SCENE_STATE_META —— 场的三态；
   · chapterStage（旧名 manuscriptStage）—— 一章走到哪一步：主页、成稿中心、章节编排同一条规则；
     chapterStarted —— 其中「动笔了没有」那一半；
   · chapterLabel / sceneLabel / *ById —— 后端 id → 「第 N 章 · 章名」「第 N 章 · 第 M 场」；
   · DRAMA_FIELDS —— 戏剧卡六格的键与叫法（编辑器、成稿中心、导出附录同一套）。
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

/* 这一章动笔了没有：已经有字，或者有一场在写 / 写完了（场上记着字数也算）。
   下面的阶段规则用它；成稿中心左栏收不收一个「规划中」的章（ws-manuscripts-store 的 manuscriptChapterEligible）
   也该按它——否则一章在页头进度里读作写作中，左栏里却没有它。 */
export function chapterStarted(chapter) {
  if (!chapter) return false;
  if (Number(chapter.words && chapter.words.cur) > 0) return true;
  return (chapter.scenes || []).some((scene) => scene && (
    scene.state === "writing" || scene.state === "done" || scene.state === "archived" || Number(scene.words) > 0));
}

/* 流程定下的阶段：审阅与终稿批准（成稿中心推进）、退回小修后的草稿。其余阶段按各场的进度读。 */
const WORKFLOW_STAGES = ["approved", "review", "draft"];

/* 一章的阶段——主页、成稿中心、章节编排同一条规则（2026-10，批准 #19）：
   已定稿 / 审阅中 / 草稿照目录；目录说「写作中」、已经有字、或者有一场在写 / 写完了，就是「写作中」；
   否则照目录说「待写」或「规划中」（认不出的状态也读作规划中）。只用于显示，绝不回写。
   以前章节编排自己一条规则：退回小修的「草稿」章读成写作中或规划中、目录说写作中但还没字的章读成规划中，
   同一章在三个视图里叫法不一样。 */
export function chapterStage(chapter) {
  const state = chapter && chapter.state;
  if (WORKFLOW_STAGES.includes(state) || state === "writing") return state;
  if (chapterStarted(chapter)) return "writing";
  return state === "todo" ? "todo" : "planned";
}

/* 这一章的阶段是不是按各场读出来的（不是审阅 / 批准 / 退回这些流程定下的）——状态标签的悬停说明据此说话 */
export function chapterStageDerived(chapter) {
  return !WORKFLOW_STAGES.includes(chapter && chapter.state);
}

/* 旧名：成稿中心、主页一直这样叫它 */
export const manuscriptStage = chapterStage;

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

/* ---------- 戏剧卡 ---------- */

/* 戏剧卡的六格：后端 narrative_json.drama 的键，编辑器（章节编排）、成稿中心的结构页签、导出的附录、
   AI 编排的补丁行都用这一套叫法（2026-10，批准 #19：以前阅读器把键改名成 thrust / turn / after，附录写「转变」「推进」）。
   group 是章节编排里的三组（承诺 / 推进 / 收束）；护栏两格（禁止包含 / 备注）不算在戏剧卡里。 */
export const DRAMA_FIELDS = [
  { key: "promise", label: "核心承诺", hint: "读完这一章读者会得到什么", group: "promise", primary: true },
  { key: "problem", label: "章节问题", hint: "本章想问读者一个什么问题", group: "promise" },
  { key: "spine", label: "主线推进", hint: "本章在全书主线上前进了多少", group: "drive" },
  { key: "arc", label: "人物变化", hint: "主要人物的内在或外在变化", group: "drive" },
  { key: "aftertaste", label: "结尾余味", hint: "读完最后一段的感觉", group: "close" },
  { key: "ending", label: "结尾效果", hint: "最后一句具体的画面 / 动作", group: "close" },
];
export const DRAMA_KEYS = DRAMA_FIELDS.map((field) => field.key);

/* 戏剧卡一格的叫法；认不出的键给空串（调用方自己兜底） */
export function dramaFieldLabel(key) {
  const field = DRAMA_FIELDS.find((f) => f.key === key);
  return field ? field.label : "";
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

/* 系统起的章名（AI 起章名只碰这些；作者起的名字不碰）——与后端 chapter_title_sync.is_auto_chapter_title
   同一口径：空、「第 N 章」（阿拉伯数字，跟着章序走的那种），或含下面任一占位标记（「未命名」= 章节编排里
   把章名清空后落下的「未命名章节」）。标记表与后端 PLACEHOLDER_TITLE_MARKERS 逐项相同。
   上面的 isPlaceholderChapterTitle 是另一条规则：只管显示时要不要把章名拼在编号后面。 */
export const PLACEHOLDER_CHAPTER_TITLE_MARKERS = ["待补", "TODO", "todo", "TBD", "tbd", "占位", "未命名"];
const AUTO_CHAPTER_TITLE_RE = /^第\s*\d+\s*章$/;
export function isAutoChapterTitle(title) {
  const text = String(title || "").trim();
  return !text || AUTO_CHAPTER_TITLE_RE.test(text) || PLACEHOLDER_CHAPTER_TITLE_MARKERS.some(m => text.includes(m));
}

/* 07 章表里「添加章节」点出来、还什么都没写的行（章名空或带占位标记，摘要 / 章目标 / 脊柱全空）——
   与后端 snowflake_chaptering.is_placeholder_chapter 同一口径。行上的章目标前端叫 goal、后端叫 chapter_goal。 */
export function isPlaceholderChapterRow(chapter) {
  const c = chapter || {};
  const title = String(c.title || "").trim();
  const blankTitle = !title || PLACEHOLDER_CHAPTER_TITLE_MARKERS.some(m => title.includes(m));
  return blankTitle && ![c.summary, c.goal, c.chapter_goal, c.spine].some(v => String(v || "").trim());
}

/* 章名框里已经带着这一章的章号吗（index 从 0 起）：章名就是「第 N 章」这种占位且 N 对得上，或者空着
   （占位提示「第 N 章（未命名）」里有章号）。这时章名框左边不再并排写一遍「第 N 章」。 */
export function chapterNoInTitle(title, index) {
  const text = String(title || "").trim();
  if (!text) return true;
  const m = /^第\s*(\d+)\s*章$/.exec(text);
  return !!(m && Number(m[1]) === index + 1);
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
