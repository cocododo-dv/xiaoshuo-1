/* ==========================================================
   主页派生（纯函数，不依赖 store，可单测）
   2026-09-16：「流程」视图并入主页。整本书逐章所在阶段（进度脊）与
   场景计数从目录真相派生；旧流程页的「流程体检」（永远只有固定一条）、
   按「略过」触发的回流条目、阶段加权百分比都不再保留——进度只留字数口径。
   2026-09-21：焦点卡、进度脊的「前线」、章节窗口都以 WsCatalog.focusScene() 为准
   （写作台 / AI 起草台用的同一条「现在该写哪一场」规则）；雪花卡只读服务端 dashboard。
   同日：章节状态不再自带一份词表。一章在哪个阶段按 ws-labels.manuscriptStage 判（规划中但已经有字
   = 写作中），叫法取 CHAPTER_STATE_META（图例、章卡、悬停说明同一个词）——与成稿中心、章节编排同一份。
   ========================================================== */
import { snowStepByBackendKey } from "./ws-nav.js";
import { CHAPTER_STATE_ORDER, chapterHeading, chapterLabel, chapterStateMeta, manuscriptStage, sceneLabel } from "./ws-labels.js";
import { LEGACY_DRAFT_PLACEHOLDER } from "./manuscript-html.js";

const HM_BEAT_TONES = ["sage", "gold", "crimson"];

/* 分段的悬停 / 读屏说明，与成稿中心进度格的 title 同一句：「第 3 章 · 章名：写作中」，前线再补一句。 */
function hmChapterHint(chapter, stage, front) {
  return `${chapterLabel(chapter, { maxTitle: Infinity })}：${chapterStateMeta(stage).label}${front ? "，前线" : ""}`;
}

/* 当前章：作者标记的 current 优先，其次第一章「在写」，最后回落到末章（与 WsCatalog.currentChapter 同规则）。
   只在拿不到焦点场景时兜底用。 */
function hmCurrentChapter(chapters) {
  const list = Array.isArray(chapters) ? chapters.filter(Boolean) : [];
  if (!list.length) return null;
  return list.find(c => c.current) || list.find(c => c.state === "writing") || list[list.length - 1];
}

function sameChapter(a, b) {
  if (!a || !b) return false;
  return a === b || (a.id != null && a.id === b.id);
}

/* 目录 → 进度脊模型：
   segments  每章一段（n / title / state / front / sid / hint），state 是 manuscriptStage 判出的阶段；
             sid 指向该章在写的场景（否则第一场没写完的；整章写完则第一场），供深链进写作房间
   counts    各阶段的章数；legend 是按 CHAPTER_STATE_ORDER 排好的图例（含 0 章的阶段）
   scenes    全书场景计数（已规划 / 已完成 / 写作中 / 待写，以及铺了场的章数）
   frontChapter 传入时「前线」就是它（主页传焦点场景所在章，焦点卡与进度脊永远指着同一章）；
   不传则按 hmCurrentChapter。认不出的章节状态按 manuscriptStage 归入规划（有字则在写）；未知的场景状态归入 todo。 */
function hmDeriveSpine(chapters, frontChapter) {
  const list = Array.isArray(chapters) ? chapters.filter(Boolean) : [];
  const cur = frontChapter ? list.find(c => sameChapter(c, frontChapter)) || null : hmCurrentChapter(list);
  const counts = {};
  CHAPTER_STATE_ORDER.forEach(k => { counts[k] = 0; });
  const scenes = { total: 0, done: 0, writing: 0, todo: 0, chapters: 0 };
  const segments = list.map(c => {
    const state = manuscriptStage(c);
    counts[state] += 1;
    const rows = Array.isArray(c.scenes) ? c.scenes.filter(Boolean) : [];
    if (rows.length) scenes.chapters += 1;
    rows.forEach(s => {
      scenes.total += 1;
      if (s.state === "done") scenes.done += 1;
      else if (s.state === "writing") scenes.writing += 1;
      else scenes.todo += 1;
    });
    /* 点一章的分段 = 进这一章：在写的那一场 → 第一场没写完的（与 WsCatalog.focusScene 同一条规则）；
       整章都写完了就从头读（第一场）——这是「进这一章」，不是「现在该写哪一场」。 */
    const target = rows.find(s => s.state === "writing") || rows.find(s => s.state !== "done") || rows[0] || null;
    const front = c === cur;
    return {
      id: c.id,
      n: c.n,
      title: c.title || "",
      state,
      front,
      sid: target && target.sid ? target.sid : "",
      hint: hmChapterHint(c, state, front),
    };
  });
  const legend = CHAPTER_STATE_ORDER.map(k => ({ state: k, label: chapterStateMeta(k).label, n: counts[k] }));
  return { total: list.length, segments, counts, legend, scenes, front: cur ? cur.n : null };
}

/* 焦点卡模型。focus 是 WsCatalog.focusScene() 的返回（{ chapter, scene, index } 或 null）；
   没有焦点场景时退回服务端 dashboard 的 home 缓存（home.slug / scene / gos）。
   slug 只放「第几章第几场」两件事；主动 / 反应另起一枚标签。 */
function hmFocusModel(focus, home) {
  const fallback = home || {};
  const scene = focus && focus.scene ? focus.scene : null;
  const chapter = focus && focus.chapter ? focus.chapter : null;
  if (!scene) {
    return {
      chapter: null, scene: null, sid: "",
      slug: String(fallback.slug || "").split(" · ").slice(0, 2).join(" · "),
      kind: "",
      title: fallback.scene || "",
      beats: Array.isArray(fallback.gos) ? fallback.gos : [],
    };
  }
  const index = typeof focus.index === "number" && focus.index >= 0 ? focus.index : 0;
  /* 三拍标签跟着场景形态走：目录给反应场景的是 反应/两难/决定（kindFields），主动场景是 目标/冲突/挫败 */
  const keys = Array.isArray(scene.kindFields) && scene.kindFields.length === 3 ? scene.kindFields : ["目标", "冲突", "挫败"];
  const beats = [scene.goal, scene.obstacle, scene.turn].map((v, i) => ({
    k: keys[i], tone: HM_BEAT_TONES[i], v: v || `（${i === 0 ? "本场" : ""}${keys[i]}待规划）`,
  }));
  return {
    chapter, scene, sid: scene.sid || "",
    slug: chapter ? sceneLabel(chapter, index) : "",
    kind: `${scene.kind || "主动"}场景`,
    title: scene.title || "未命名场景",
    beats,
  };
}

/* 旧占位句自己的字数（编辑器口径：去掉空白后的字符数）。后端还不认识这句占位，旧草稿按它计过字。 */
const hmCharCount = (s) => String(s == null ? "" : s).replace(/\s/g, "").length;
const HM_PLACEHOLDER_WORDS = hmCharCount(LEGACY_DRAFT_PLACEHOLDER);

/* 服务端 dashboard 的 last_lines：后端从草稿纯文本切出来的末两行，没去旧占位。
   只有这几行就是整份草稿时，第一行才是草稿的开头——一行时必然如此；两行时拿同一份草稿的字数对一下
   （同样是去空白后的字符数），对不上就看不出来，不动。开头的占位按写作台同一条规则去掉：
   整行是占位就删行，占位后面接着写的只删这几个字；正文后面恰好写到这句话，是作者的字。 */
function hmDashboardLines(lines, draftWords) {
  const list = (Array.isArray(lines) ? lines : []).map(x => String(x == null ? "" : x).trim()).filter(Boolean);
  const whole = list.length === 1 || (list.length > 1 && list.reduce((n, x) => n + hmCharCount(x), 0) === draftWords);
  if (!whole || !list[0].startsWith(LEGACY_DRAFT_PLACEHOLDER)) return { lines: list, placeholderOnly: false };
  const rest = list[0].slice(LEGACY_DRAFT_PLACEHOLDER.length).trim();
  const out = rest ? [rest, ...list.slice(1)] : list.slice(1);
  return { lines: out, placeholderOnly: out.length === 0 };
}

/* 「上次写到这里」。
   cached：写作台落盘缓存读出来的 { lines, placeholderOnly }（lines 已去掉开头的旧占位；没有缓存或读不到为 null）；
   resume：dashboard 的 home.resume（lines / pausedAgo / sceneWords）；sceneWords：目录里这一场的字数。
   · 本机缓存里有字就用它（可能比服务端新，还没存上去）；「暂停于」是服务端草稿的时间，配不上本机的句子，不显示。
   · 本机缓存没字（没有缓存、读不到，或者只剩旧占位）才看服务端的——缓存是服务端草稿的镜像，
     可别的设备后来写下的字只有服务端有，所以不拿一份空缓存去盖服务端的字；服务端的行同样去掉开头的旧占位。
   · 草稿里只有旧占位：说还没有正文，不写「暂停于」；字数正好是占位自己的字数（旧版按它计过）时显示 0，
     免得「本场 11 字」和「这一场还没有正文」并排。 */
function hmResumeModel(cached, resume, sceneWords) {
  const r = resume || {};
  const dashWords = typeof r.sceneWords === "number" ? r.sceneWords : 0;
  const words = typeof sceneWords === "number" ? sceneWords : dashWords;
  if (cached && Array.isArray(cached.lines) && cached.lines.length) {
    return { lines: cached.lines.slice(-2), pausedAgo: "", words };
  }
  const dash = hmDashboardLines(r.lines, dashWords);
  const placeholderOnly = dash.placeholderOnly || !!(cached && cached.placeholderOnly && !dash.lines.length);
  return {
    lines: dash.lines.slice(-2),
    pausedAgo: dash.lines.length ? (r.pausedAgo || "") : "",
    words: placeholderOnly && words === HM_PLACEHOLDER_WORDS ? 0 : words,
  };
}

/* 「当前章附近」：前线章的前一章到后三章（共五章）；靠近书尾时往前补足。
   每张章卡带和进度脊一样的深链 sid，状态标签用阶段的叫法与语气色（ws-ui Tag 的 tone）。 */
const HM_WINDOW_BEFORE = 1;
const HM_WINDOW_AFTER = 3;

function hmChapterWindow(chapters, spine) {
  const list = Array.isArray(chapters) ? chapters.filter(Boolean) : [];
  const size = HM_WINDOW_BEFORE + 1 + HM_WINDOW_AFTER;
  const segs = (spine && spine.segments) || [];
  let frontIdx = segs.findIndex(s => s.front);
  if (frontIdx < 0) frontIdx = 0;
  let start = Math.max(0, frontIdx - HM_WINDOW_BEFORE);
  start = Math.max(0, Math.min(start, list.length - size));
  const cards = list.slice(start, start + size).map((c, i) => {
    const seg = segs[start + i] || {};
    const state = manuscriptStage(c);
    const meta = chapterStateMeta(state);
    // 章卡两段：num「第 N 章」与章自己的名字；占位名（第 N 章 / 未命名）不再跟章号并排
    const head = chapterHeading(c);
    return {
      id: c.id,
      n: c.n,
      num: head.num,
      title: head.title,
      state,
      stateLabel: meta.label,
      tone: meta.tone,
      pct: c.words && c.words.target ? Math.min(100, Math.round(((c.words.cur || 0) / c.words.target) * 100)) : 0,
      front: !!seg.front,
      sid: seg.sid || "",
    };
  });
  return { cards, partial: list.length > size };
}

/* 雪花卡：只读服务端 dashboard 的十步状态（done / active / warn / todo）。
   过去这里优先读构思页的本机缓存，于是同一张卡在打开过构思与没打开过时说法不一样。
   next：还有没确认的步骤 → 回那一步继续；十步都确认了 → 去写焦点场景。 */
function hmSnowSummary(snow) {
  const raw = Array.isArray(snow) ? snow.filter(Boolean) : [];
  const steps = raw.map((s) => {
    const meta = s.key ? snowStepByBackendKey(s.key) : null;
    return { name: s.name || (meta && meta.short) || "", s: s.s || "todo", key: meta ? meta.key : "", num: meta ? meta.num : "" };
  });
  const total = steps.length;
  const done = steps.filter(s => s.s === "done").length;
  const allDone = total > 0 && done === total;
  const current = allDone ? null : (steps.find(s => s.s === "active") || steps.find(s => s.s !== "done") || null);
  let now = "";
  if (allDone) now = "十步已全部确认";
  else if (current) now = current.num ? `${current.name} · 第 ${Number(current.num)} 步` : current.name;
  let next = null;
  if (allDone) next = { action: "write", label: "去写这一场" };
  else if (current) next = { action: "step", key: current.key, label: "继续构思" };
  return { steps, total, done, allDone, now, next };
}

/* 雪花卡没有步骤可显示时，是「还在读」「读不到」还是「读到了但没有」：
   过去只凭步骤为空一律说「正在读取」，dashboard 读失败时这张卡就永远在转圈，而页顶已经在说数据没更新。
   idle 是还没开始拉——作品列表在读时 store 读完会接着拉；列表读失败就不会再拉了，算读不到。 */
function hmSnowLoadState(remote) {
  const dashboard = (remote && remote.dashboard) || {};
  const projects = (remote && remote.projects) || {};
  if (dashboard.phase === "loading") return "loading";
  if (dashboard.phase === "error" || dashboard.error) return "error";
  if (dashboard.phase === "ready") return "empty";
  if (projects.phase === "error") return "error";
  return "loading";
}

/* 全书进度的说法：「动笔 N / M 章」——N 是已经有正文的章，M 是目录里的章数。
   和进度脊的图例（已定稿 / 审阅中 / 草稿 …）是两种口径，所以不叫「完成」。 */
function hmBookProgress(totals, work) {
  const words = (totals && totals.words) || 0;
  const target = Math.max(1, (work && work.wordsTarget) || 0);
  return {
    written: (totals && totals.written) || 0,
    planned: (totals && totals.planned) || 0,
    pct: Math.min(100, Math.round((words / target) * 100)),
    wordsWan: (words / 10000).toFixed(1),
    targetWan: ((work && work.wordsTarget) / 10000 || 0).toFixed(0),
  };
}

export {
  hmBookProgress, hmChapterWindow, hmCurrentChapter, hmDeriveSpine, hmFocusModel, hmResumeModel, hmSnowLoadState, hmSnowSummary,
};
