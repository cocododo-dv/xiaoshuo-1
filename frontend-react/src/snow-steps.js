/* ==========================================================
   snow-steps — 雪花十步的步骤目录（叶子模块：不 import 任何东西）
   ----------------------------------------------------------
   前端步骤键（ws:snow-step 的 detail）、后端 step_key、序号、全名与主页 / 命令面板上的短名、
   所在轨道（情节 / 角色 / 定位）、是不是整理章节结构之前必须确认的一步，以及展开来源（依赖 DAG：
   fromKey 是主展开源，alsoFrom 是跨轨依赖）。以前这张表有三份（外壳导航 ws-nav 一份、构思视图的
   模型一份、后端一份），前端两份要靠一条单测对齐；现在外壳导航与构思视图都从这里取，
   构思视图只在上面加写作指引的文案（ws-snow-model.js）。后端 step_key 仍以后端为准。
   ========================================================== */

export const SNOW_STEPS = [
  { key: "audience",   be: "book_brief",            num: "01", name: "读者定位",   short: "读者定位",   track: "orient",    essential: true },
  { key: "logline",    be: "one_sentence_summary",  num: "02", name: "一句话概括", short: "一句话",     track: "plot",      essential: true,  fromKey: "audience" },
  { key: "paragraph",  be: "one_paragraph_summary", num: "03", name: "一段话概括", short: "一段话",     track: "plot",      essential: true,  fromKey: "logline" },
  { key: "characters", be: "character_sheets",      num: "04", name: "角色摘要表", short: "角色摘要",   track: "character", essential: false, fromKey: "paragraph" },
  { key: "synopsis",   be: "short_synopsis",        num: "05", name: "一页梗概",   short: "一页梗概",   track: "plot",      essential: false, fromKey: "paragraph", alsoFrom: ["characters"] },
  { key: "backstory",  be: "character_synopses",    num: "06", name: "角色背景",   short: "角色背景",   track: "character", essential: false, fromKey: "characters" },
  { key: "outline",    be: "long_synopsis",         num: "07", name: "长篇大纲",   short: "长篇大纲",   track: "plot",      essential: false, fromKey: "synopsis" },
  { key: "profile",    be: "character_bibles",      num: "08", name: "角色全档案", short: "角色全档案", track: "character", essential: false, fromKey: "backstory" },
  { key: "scenes",     be: "scene_list",            num: "09", name: "场景列表",   short: "场景列表",   track: "plot",      essential: true,  fromKey: "outline", alsoFrom: ["characters"] },
  { key: "planning",   be: "scene_details",         num: "10", name: "场景规划",   short: "场景规划",   track: "plot",      essential: true,  fromKey: "scenes" },
];

/* 后端 step_key → 目录条目；认不出返回 null */
export function snowStepByBackendKey(beKey) {
  return SNOW_STEPS.find(s => s.be === beKey) || null;
}
