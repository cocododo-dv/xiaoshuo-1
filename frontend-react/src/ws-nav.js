/* ==========================================================
   导航模型（纯数据，ESM，不写 window）
   侧栏、命令面板、路由校验共用这一份。过去命令面板手抄了一份页面清单，
   漏了「文学质量」「成本看板」，在 ⌘K 里怎么搜都找不到。
   路由本身（hash 读写、别名重定向、懒加载）仍在 ws-app.jsx。
   ========================================================== */

/* 导航分组：日常写作永远在；生产与质控、运维工具只在高级模式出现，让作家模式的侧栏保持安静。
   「系统」组（设置 / 回收站）不进可滚动的导航区，固定在侧栏底部。
   desc 是命令面板里的一句说明；kw 是拼音 / 英文检索词（面板按它模糊匹配）。 */
const WS_NAV_GROUPS = [
  {
    id: "daily", label: "日常写作",
    items: [
      { id: "home",      label: "主页", icon: "Home",      desc: "今天从哪一场写起", kw: "home zhuye shouye" },
      { id: "snowflake", label: "构思", icon: "Snowflake", desc: "雪花十步",         kw: "snowflake gousi xuehua" },
      { id: "writer",    label: "写作", icon: "Pen",       desc: "写作房间",         kw: "writer xiezuo fangjian" },
      { id: "styleref",  label: "风格", icon: "Beaker",    desc: "参考书、学习文风、用于作品、对照检查（像不像）", kw: "styleref fengge cankaoshu duizhao xiangbuxiang" },
      { id: "review",    label: "待办", icon: "Inbox",     desc: "待办收件箱",       kw: "review daiban shoujianxiang", liveBadge: true },
      { id: "library",   label: "资料", icon: "Library",   desc: "人物、设定与大事记", kw: "library ziliao renwu sheding" },
    ],
  },
  {
    id: "production", label: "生产与质控", advanced: true,
    items: [
      { id: "author",      label: "章节编排", icon: "Layout",     desc: "章与场的结构", kw: "author zhangjie bianpai" },
      { id: "scene",       label: "AI 起草台", icon: "Play",       desc: "按场起草",     kw: "scene ai qicao changjing" },
      { id: "manuscripts", label: "成稿中心", icon: "BookOpen",   desc: "定稿与导出",   kw: "manuscripts chenggao dinggao daochu" },
      { id: "quality",     label: "文学质量", icon: "Microscope", desc: "质量体检",     kw: "quality wenxue zhiliang" },
    ],
  },
  {
    id: "ops", label: "运维工具", advanced: true,
    items: [
      { id: "cost", label: "成本看板", icon: "Coins", desc: "模型用量与费用", kw: "cost chengben kanban yongliang feiyong" },
    ],
  },
  {
    id: "system", label: "系统",
    items: [
      { id: "settings", label: "设置",   icon: "Settings", desc: "模型、外观与数据", kw: "settings shezhi" },
      { id: "trash",    label: "回收站", icon: "Trash",    desc: "删掉的内容在这里找回", kw: "trash huishouzhan" },
    ],
  },
];

const WS_SYSTEM_GROUP = "system";

const WS_NAV_ITEMS = WS_NAV_GROUPS.flatMap(g => g.items.map(it => ({ ...it, group: g.id, advanced: !!g.advanced })));

// flat id → label, for routing validation + palette
const WS_VIEW_LABELS = Object.fromEntries(WS_NAV_ITEMS.map(it => [it.id, it.label]));
const WS_ALL_VIEWS = WS_NAV_ITEMS.map(it => it.id);

/* 必须先有一部作品才能打开的页面（没有时显示「先创建一部作品」） */
const WS_PROJECT_SCOPED_VIEWS = new Set([
  "snowflake", "writer", "library", "author", "scene",
  "manuscripts", "quality",
]);

/* 旧路由别名：独立深改台已并入写作台，深链重定向到深改姿态；
   「流程」视图（#flowmap）于 2026-09-16 并入主页（全书进度脊 + 场景计数），深链回主页 */
const WS_VIEW_ALIAS = { deepdesk: "writer", flowmap: "home" };

function isKnownView(view) {
  return WS_ALL_VIEWS.includes(view);
}

function isAdvancedView(view) {
  return WS_NAV_ITEMS.some(it => it.id === view && it.advanced);
}

/* 侧栏可滚动区里的分组（系统组固定在底部，另取） */
function navGroupsForMode(mode) {
  return WS_NAV_GROUPS.filter(g => g.id !== WS_SYSTEM_GROUP && (!g.advanced || mode === "advanced"));
}

function systemNavGroup() {
  return WS_NAV_GROUPS.find(g => g.id === WS_SYSTEM_GROUP) || null;
}

/* 雪花十步：前端步骤键（ws:snow-step 的 detail）、后端 step_key、序号、全名与主页上的短名。 */
const WS_SNOW_STEPS = [
  { key: "audience",   be: "book_brief",            num: "01", name: "读者定位",   short: "读者定位" },
  { key: "logline",    be: "one_sentence_summary",  num: "02", name: "一句话概括", short: "一句话" },
  { key: "paragraph",  be: "one_paragraph_summary", num: "03", name: "一段话概括", short: "一段话" },
  { key: "characters", be: "character_sheets",      num: "04", name: "角色摘要表", short: "角色摘要" },
  { key: "synopsis",   be: "short_synopsis",        num: "05", name: "一页梗概",   short: "一页梗概" },
  { key: "backstory",  be: "character_synopses",    num: "06", name: "角色背景",   short: "角色背景" },
  { key: "outline",    be: "long_synopsis",         num: "07", name: "长篇大纲",   short: "长篇大纲" },
  { key: "profile",    be: "character_bibles",      num: "08", name: "角色全档案", short: "角色全档案" },
  { key: "scenes",     be: "scene_list",            num: "09", name: "场景列表",   short: "场景列表" },
  { key: "planning",   be: "scene_details",         num: "10", name: "场景规划",   short: "场景规划" },
];

function snowStepByBackendKey(beKey) {
  return WS_SNOW_STEPS.find(s => s.be === beKey) || null;
}

export {
  WS_NAV_GROUPS, WS_NAV_ITEMS, WS_SYSTEM_GROUP, WS_VIEW_LABELS, WS_ALL_VIEWS,
  WS_PROJECT_SCOPED_VIEWS, WS_VIEW_ALIAS, WS_SNOW_STEPS,
  isKnownView, isAdvancedView, navGroupsForMode, systemNavGroup, snowStepByBackendKey,
};
