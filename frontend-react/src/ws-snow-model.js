/* ==========================================================
   雪花十步的数据与纯函数（叶子模块：不 import 任何东西）
   ----------------------------------------------------------
   步骤目录、前后端步骤键映射、每一步的写作指引、空白脚手架与缓存归一、以及视图与同步层
   共用的确定性推导（覆盖格、结构核对、失效图、节奏 / 织线统计……）。
   视图（ws-snow*.jsx）和同步层（ws-snow-sync.jsx）都从这里取；以前同步层反过来 import 整张
   视图模块，视图只好经 window.SnowSync 绕开那个循环——这份叶子模块把循环拆掉了。
   ========================================================== */

/* 十步目录。book / grow / timebox 是原著的位置、展开倍率与建议用时；fromKey / alsoFrom 是展开来源（依赖 DAG）。 */
export const S2_STEPS = [
  { key: "audience",  num: "01", name: "读者定位",   blurb: "为谁写、读者期待哪种快感",     essential: true,  track: "orient",    book: "原著之前的定锚", grow: "锚定目标读者", timebox: "30 分钟" },
  { key: "logline",   num: "02", name: "一句话概括", blurb: "全书核心冲突压成一行",         essential: true,  track: "plot",      book: "原著第 1 步",    grow: "1 句 · 约 40 字", timebox: "1 小时", from: "读者定位", fromKey: "audience" },
  { key: "paragraph", num: "03", name: "一段话概括", blurb: "五句 = 三幕骨架 + 三大灾难",   essential: true,  track: "plot",      book: "原著第 2 步",    grow: "1 句 → 5 句",   timebox: "1 小时", from: "一句话概括", fromKey: "logline" },
  { key: "characters",num: "04", name: "角色摘要表", blurb: "目标·抱负·价值观·阻碍·顿悟",   essential: false, track: "character", book: "原著第 3 步",    grow: "每人 1 张表",   timebox: "每人 1 小时", from: "一段话概括", fromKey: "paragraph" },
  { key: "synopsis",  num: "05", name: "一页梗概",   blurb: "把每一句扩成一段",            essential: false, track: "plot",      book: "原著第 4 步",    grow: "5 句 → 1 页",   timebox: "1 小时", from: "一段话概括", fromKey: "paragraph", alsoFrom: ["characters"] },
  { key: "backstory", num: "06", name: "角色背景",   blurb: "每个主要角色的来路与伤",       essential: false, track: "character", book: "原著第 5 步",    grow: "每人半页",     timebox: "每人 1 小时", from: "角色摘要表", fromKey: "characters" },
  { key: "outline",   num: "07", name: "长篇大纲",   blurb: "把一页扩成四页",              essential: false, track: "plot",      book: "原著第 6 步",    grow: "1 页 → 4 页",   timebox: "2 小时", from: "一页梗概", fromKey: "synopsis" },
  { key: "profile",   num: "08", name: "角色全档案", blurb: "生理·心理·环境·性格全维度",    essential: false, track: "character", book: "原著第 7 步",    grow: "每人完整档案", timebox: "每人数小时", from: "角色背景", fromKey: "backstory" },
  { key: "scenes",    num: "09", name: "场景列表",   blurb: "一行一场 · 每场都要有冲突",    essential: true,  track: "plot",      book: "原著第 8 步",    grow: "全书拆成场",   timebox: "几天", from: "长篇大纲", fromKey: "outline", alsoFrom: ["characters"] },
  { key: "planning",  num: "10", name: "场景规划",   blurb: "主动：目标 / 冲突 / 挫败\n反应：反应 / 两难 / 决定", essential: true,  track: "plot",      book: "原著第 9 步",    grow: "每场 5 分钟",   timebox: "每场 5 分钟", from: "场景列表", fromKey: "scenes" },
];

export const TRACK_LABEL = { plot: "情节", character: "角色", orient: "定位" };

/* 步骤状态的中文名（导出大纲、进度刻度、步骤列表共用） */
export const S2_STATE_LABEL = { done: "已确认", warn: "需补", active: "进行中", skip: "已略过", todo: "待写", stale: "需复核" };

/* FE→BE 步骤键映射（正源；ws-snow-sync 复用同一份避免漂移） */
export const S2_BE_STEPS = [
  ["audience", "book_brief"],
  ["logline", "one_sentence_summary"],
  ["paragraph", "one_paragraph_summary"],
  ["characters", "character_sheets"],
  ["synopsis", "short_synopsis"],
  ["backstory", "character_synopses"],
  ["outline", "long_synopsis"],
  ["profile", "character_bibles"],
  ["scenes", "scene_list"],
  ["planning", "scene_details"],
];
export const S2_BE_KEY = Object.fromEntries(S2_BE_STEPS);

/* 外部跳转带来的步骤键：前端键原样认；后端 step_key 换成前端键；认不出返回 "" */
export function s2FindStepKey(key) {
  const k = String(key || "");
  if (S2_STEPS.some(s => s.key === k)) return k;
  const pair = S2_BE_STEPS.find(([, be]) => be === k);
  return pair ? pair[0] : "";
}

/* The story's structural spine — three disasters + the moral premise
   that flips at the midpoint. Shown in the right rail on plot steps. */
export const S2_DISASTERS = [
  { id: "灾一", act: "第一幕末", tone: "crimson" },
  { id: "灾二", act: "第二幕中点", tone: "gold" },
  { id: "灾三", act: "第二幕末", tone: "crimson" },
];

/* 五个回头自问（Ingermanson 的分形精神）。以前它们是一把按关键词计数打分的「实时自评」尺子——
   数「因为 / 所以 / 读者」出来的 0–100 分，常常和后端的权威评定打架。现在它们只是写作指引里的
   自问，不打分；本步唯一的评分是后端完备性闸门的评定（右栏「本步检查」）。 */
export const S2_RUBRIC = [
  { k: "分形一致", q: "展开后回头压缩——上一层的概括是否仍然成立？" },
  { k: "因果锁链", q: "每个事件是否锁死下一个走向——更难、更贵、更不可逆？" },
  { k: "角色驱动", q: "是角色的选择在推动情节，还是你在替角色做决定？" },
  { k: "可落场景", q: "拆到最小单元时，每一场都有人想要什么、有什么挡着？" },
  { k: "读者契约", q: "你承诺给读者的那种快感，这一层是否还在兑现？" },
];

/* 每一步的写作指引与编辑形态：target 是建议字数，scaffold 是结构化编辑器的种类，
   guide 进右栏（本步任务 / 写作指引 / 写完对一遍的清单）。 */
export const S2_STEP_DATA = {
  audience: {
    target: 200,
    scaffold: { type: "audience" },
    guide: {
      task: "雪花从定锚开始：你为谁写？她要哪种快感？后面九步每一次展开、每一条取舍，都拿这把尺子量。",
      writing: [
        { k: "类型即承诺", v: "先定类型——文学悬疑、言情、硬推理……类型决定了读者带着什么期待打开书" },
        { k: "一句话快感", v: "用「她读完会觉得 ___」一句话锁定核心快感" },
        { k: "敢于排除", v: "反向定位比正向更有力——写下「我不为谁写」，砍掉犹豫" },
      ],
      checklist: [
        "能一句话说出读者要的核心快感。",
        "明确写下了「不写什么 / 不为谁写」。",
        "后面九步的每一个取舍，都能拿这条来裁决。",
      ],
      note: "Ingermanson：你只有一种读者——你的目标读者。取悦她，忘掉其他人。",
    },
  },
  logline: {
    // 长度只有一个口径：约 40 字（与后端提示词、分诊同一契约）。原书的「≤ 25 个英文词」只作注脚。
    target: 40,
    meter: { target: 40, note: "约 40 字最易记住，也最像一句宣传语（原书说英文不超过 25 个词）。" },
    guide: {
      task: "雪花的种子：把整部小说压缩成一句话。这是分形的原点——后面所有层都从它长出来。也是你最强的营销工具：让人听完就想说「告诉我更多」。",
      writing: [
        { k: "因果句公式", v: "「一位[有特点的角色]必须[达成目标]，但[核心障碍挡着她]。」——主角、目标、阻力、代价，一句话装下" },
        { k: "悬念不泄底", v: "暗示有赌注，但把结局和最大反转藏住" },
        { k: "能脱口而出", v: "短到你能在电梯里说完——中文 ≤ 40 字" },
      ],
      checklist: [
        "一句因果句里保留了主角、目标、阻力和代价。",
        "结局和反转都藏住了。",
        "大声念一遍，能让人接一句「然后呢？」",
      ],
      note: "这句话既是创作罗盘，也是将来印在书封上的那行字。写不出它，说明故事还没想清楚。",
    },
  },
  paragraph: {
    target: 300,
    scaffold: { type: "beats" },
    guide: {
      task: "第一次分形展开：一句话长成五句话。五句话 = 三幕骨架——铺垫、三个逐级升高的灾难、结局。全书的脊柱在这一步立直。",
      writing: [
        { k: "五句五节点", v: "①背景与主角登场 → ②灾难一：被迫卷入，无法回头 → ③灾难二：世界观被打碎 → ④灾难三：局势失控，逼向终局 → ⑤决战与收尾" },
        { k: "灾难逐级升高", v: "每个灾难都改变主角下一步能做什么——代价更大、退路更少、选择更不可逆" },
        { k: "道德前提翻转", v: "灾难二是中点：主角的错误信念碎掉，正确信念开始生长" },
      ],
      checklist: [
        "三个灾难一个比一个狠，退路逐级收窄。",
        "灾难二处主角的核心信念发生翻转。",
        "结局回应了铺垫埋下的赌注。",
      ],
      note: "脊柱歪了，后面长多少肉都是歪着长。改五句话只要十分钟；改十万字的初稿要十个月。",
    },
  },
  characters: {
    target: 240,
    scaffold: { type: "charsheet" },
    guide: {
      task: "情节与角色交替展开——第一次切到角色轨道。角色的价值观冲突产生你所有的场景冲突。给每人一张摘要表：目标、抱负、价值观、阻碍、顿悟，再加她自己的一句话 / 一段话故事线。",
      writing: [
        { k: "目标要具体", v: "写看得见、可验证的东西——不是「寻找自我」，是「查清谁改了档案」" },
        { k: "价值观要碰撞", v: "用「没有什么比 ___ 更重要」写 2–3 条，互相有张力——主角和对手这句话必须冲突" },
        { k: "反派也是主角", v: "每个角色都是自己故事的主角，包括反派——在她自己的故事里，她也是对的" },
        { k: "她自己的故事线", v: "一句话 + 一段话：在她自己的故事里她要什么、谁挡着、三次灾难怎样打在她身上、她的结局" },
      ],
      checklist: [
        "主角与对手都有具体的、可验证的目标；配角可以留白，不编。",
        "主角和对手的价值观正面对撞。",
        "反派的逻辑在她自己看来说得通。",
      ],
      note: "故事 = 角色被丢进坩埚。坩埚的温度，取决于你把对手写得多认真。",
    },
  },
  synopsis: {
    target: 800,
    scaffold: { type: "synopsisbeats" },
    guide: {
      task: "第二次分形展开：五句话的每一句变成一段，长成约一页梗概。这是故事第一次「填肉」——做法机械但极可靠。",
      writing: [
        { k: "一句变一段", v: "03 第 N 句 → 第 N 段：①世界观与初始冲突 ②触发事件与灾难一 ③挣扎与灾难二的认知翻转 ④升级与灾难三 ⑤高潮走向与收尾" },
        { k: "每段要有画面", v: "别写概括——给具体的时间、地点、在场的人、行动与反应" },
        { k: "结尾留钩", v: "每段结尾制造「必须翻到下一段」的牵引力" },
      ],
      checklist: [
        "每一段都严格对应 03 的一句话。",
        "每一段都有一个看得见的具体场景。",
        "结尾有让人「必须继续」的钩子。",
      ],
      note: "这一页就是你的故事提案。讲不清一页，就讲不清四百页。",
    },
  },
  backstory: {
    target: 800,
    scaffold: { type: "backstory" },
    guide: {
      task: "角色轨道的第二次展开：为每人写半页来路。不是编户口簿——而是找到那件把她变成今天这个人的事。理解角色为何如此，你才能写出真实可信的行动。",
      writing: [
        { k: "信念的起点", v: "哪些关键事件塑造了她的性格？故事开始前她相信什么？" },
        { k: "内心世界", v: "她真正渴望的是什么？为何渴望？最害怕被人发现什么？" },
        { k: "关系与行为", v: "她与其他角色的纠葛；她在压力下会表现出什么行为？" },
      ],
      checklist: [
        "能回答「她为什么变成了现在这个人」。",
        "背景能解释她在正文里的每一个关键选择。",
        "反派的来路写得同样认真。",
      ],
      note: "写不好背景，你就只能让角色听你指挥。写好了，她会自己做决定。",
    },
  },
  outline: {
    target: 600,
    scaffold: { type: "chapters" },
    guide: {
      task: "第三次分形展开：一页梗概的每一段再长成一页，得到四五页的长篇梗概。这是最接近实际写作的规划阶段。",
      writing: [
        { k: "一段变一页", v: "05 的每一段 → 这里的一页：加入具体场景设定、角色行动与反应、关键对话要点、情感变化节点" },
        { k: "灾难定位", v: "三个灾难必须落在幕与幕的交界——它们是结构的铰链" },
        { k: "章表可以后补", v: "章是列完场之后的包装决定：章节表可以先空着，09 场景列好后「整理章节结构」按场景分章，确认的章表会回填到这里" },
      ],
      checklist: [
        "五段都扩成了约一页，每一段都比 05 的源段多出画面与行动。",
        "三个灾难在展开里有明确位置。",
        "写了章节表的话：每一章都推动了局面，没有原地打转（章表也可以留空）。",
      ],
      note: "五段展开是场景列表的上游：09 的每一场都要能追溯到这里的一段。章怎么分，等场列出来再定。",
    },
  },
  profile: {
    target: 700,
    scaffold: { type: "profile" },
    guide: {
      task: "角色的终极展开：为每人建一份「角色圣经」。写完后你应该能替她回答任何问题——因为她在你脑子里活了。这是雪花最深的一层角色挖掘。",
      writing: [
        { k: "四个维度", v: "生理（外貌、习惯）· 心理（恐惧、渴望）· 环境（家庭、工作）· 性格（口头禅、矛盾面）" },
        { k: "矛盾比一致重要", v: "人物的魅力来自矛盾——她嘴上说的和实际做的不一样" },
        { k: "两个版本的她", v: "「别人眼中的她」和「她自己眼中的她」——落差就是人物弧光的起点" },
      ],
      checklist: [
        "四个维度都有具体内容，不只是标签。",
        "至少写出了一个人物内在矛盾。",
        "「别人看她」和「她看自己」之间有落差。",
      ],
      note: "Ingermanson：到这一步你应该对角色了如指掌。找一张「长得像她」的照片贴在桌旁。",
    },
  },
  scenes: {
    target: 500,
    scaffold: { type: "scenelist" },
    guide: {
      task: "分形展开接近底层：把大纲拆成一行一场的清单。场景是小说的基本单位——每个场景必须有冲突，必须是一个完整的缩微故事。",
      writing: [
        { k: "一行一场", v: "编号 · 类型（主动/反应）· 视角人物 · 地点/时间 · 坩埚（困住角色的力量）· 结果/转变" },
        { k: "铁律三条", v: "①每场必须有冲突 ②没有冲突的场景→删除 ③一场挫败后三选一：换视角线 / 直接下一场主动 / 下一目标不明显时才写反应场——反应场是少数，不要机械交替" },
        { k: "视角与坩埚", v: "视角选这一场里损失最大的人；每场的坩埚都要是新的，不重复上一场的困局" },
        { k: "情绪节奏", v: "相邻两场温度要有起伏，不能全程高温也不能全程低温" },
      ],
      checklist: [
        "每一场都有明确冲突。",
        "没有只交代背景的「死场」。",
        "连续读下来，情绪有起有伏。",
      ],
      note: "冲突是让故事跑起来的汽油。场景没冲突，就是一辆抛锚的车——推它不如砍它。",
    },
  },
  planning: {
    target: 400,
    scaffold: { type: "scene" },
    guide: {
      task: "雪花的最后一步：给每场花五分钟画草图。主动场景制造紧张；反应场景让人物消化挫败、做出下一个决定——它是少数，可以整场写，也可以缩成两段概述。",
      writing: [
        { k: "主动场景", v: "目标（具体可拍摄）→ 冲突（多轮受阻）→ 挫败（结尾比开场更糟，迫使翻页）" },
        { k: "反应场景", v: "反应（情感先于理性，用身体呈现）→ 两难（每个选项都有代价）→ 决定（触发下一场目标）" },
        { k: "链条", v: "挫败接反应、或直接接下一个目标；决定接目标——链条不能断，但不要机械交替。挫败以主角衡量：视角是对手时，对手得手就是挫败" },
      ],
      checklist: [
        "标明了主动 / 反应类型。",
        "三个槽位都填满了。",
        "结尾自然接上下一场的开头。",
      ],
      note: "十步做完，设计阶段结束。从现在起你脑子里只剩一件事：把它写好看。",
    },
  },
};

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

// 09 场景列表的结构核对：只读织线 / 节奏的确定性结构，不打分（与画布上的诊断同源）。
// 「支线织入并折射主题」曾在这里——只有主线的书永远不过，那是一条意见，不是结构事实，已去掉。
export function s2SceneAuto(scaffold) {
  const list = (scaffold && scaffold.list) || [];
  const lines = (scaffold && scaffold.lines) || [];
  const pacing = s2PacingRuns(list);
  const stats = s2LineStats(list, lines);
  const noCru = list.filter(s => !(s.crucible || "").trim()).length;
  const tightMax = pacing.tight.length ? Math.max(...pacing.tight.map(r => r.len)) : 0;
  const clustered = stats.filter(s => s.clustered).length;
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
/* 第 10 步旧数据形状（全书只有一张 GCS/RDD 表）→ 逐场 plans 形状的一次性归一 */
export const S2_PLAN_FIELDS = ["goal", "conflict", "setback", "reaction", "dilemma", "decision"];
function s2NormalizePlanning(p) {
  if (!p) return { sel: "", plans: {} };
  const legacyAny = S2_PLAN_FIELDS.some(f => (p[f] || "").trim());
  const out = { sel: p.sel || "", plans: { ...(p.plans || {}) } };
  if (legacyAny && !p.plans) {
    // 纯旧形状：把那张表挂到它标注的场景 id 下（解不出则 S01）
    const m = /S\d+/.exec(p.scene || "");
    const id = m ? m[0] : "S01";
    const plan = { mode: p.mode || "proactive", pov: p.pov || "" };
    S2_PLAN_FIELDS.forEach(f => { plan[f] = p[f] || ""; });
    out.plans[id] = plan;
    out.sel = id;
  }
  if (!out.sel) out.sel = Object.keys(out.plans)[0] || "";
  return out;
}
export function s2MergeScaffolds(stored) {
  const base = s2BlankScaffolds();
  if (stored) Object.keys(base).forEach(k => {
    if (!stored[k]) return;
    base[k] = { ...base[k], ...stored[k] };
    if (base[k].chars && stored[k].chars) base[k].chars = { ...base[k].chars, ...stored[k].chars };
    if (k === "planning" && base[k].plans && stored[k].plans) base[k].plans = { ...base[k].plans, ...stored[k].plans };
  });
  base.planning = s2NormalizePlanning(base.planning);
  return base;
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
  delete source.revs; delete source.confirmRevs; // E3 第二步：旧缓存里的本地失效图直接丢弃
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
