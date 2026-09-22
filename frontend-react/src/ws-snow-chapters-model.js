/* ==========================================================
   分章面板的纯模型（从 ws-snow-chapters.jsx 拆出，2026-09-22）
   ----------------------------------------------------------
   preview 回包 → 面板本地态、本地态 → 提交载荷，以及挪章界 / 拆章 / 并章 / AI 起章名这些
   不碰 React、不读 store 的纯函数。面板（ws-snow-chapters.jsx）只管状态与交互，
   章与场的展示件在 ws-snow-chapters-parts.jsx。章是场景列表上连续的一段——
   这里的每一个改动都守着这条纪律；分章算法本身在后端（snowflake_chaptering.py）。
   ========================================================== */

export const ACT_LABEL = { 1: "第一幕", 2: "第二幕", 3: "第三幕" };

const NEW_PREFIX = "new:";
let newChapterCounter = 0;
const mintChapterKey = () => `${NEW_PREFIX}fe${Date.now().toString(36)}${(newChapterCounter++).toString(36)}`;
export const isNewChapter = (chapter) => String((chapter && chapter.rowUid) || "").startsWith(NEW_PREFIX);

const shapeScene = (s, fallbackForm) => ({
  scenePlanId: s.scene_plan_id,
  sceneId: s.scene_id || "",
  title: s.title || "",
  summary: s.summary || "",
  fn: s.function || "",
  storyIndex: Number(s.story_index) || 0,
  primaryForm: s.primary_form || fallbackForm || "proactive",
  spine: s.spine || "",
  anchored: !!s.anchored,
  planned: !!s.planned,
});

/* preview 回包 → 面板可编辑的本地态。改动只在本地，确认前不落库。 */
export function shapeDraft(preview) {
  return {
    strategy: (preview && preview.strategy) || "spine_anchor",
    chapters: ((preview && preview.chapters) || []).map(c => ({
      rowUid: c.row_uid,
      title: c.title || "",
      act: c.act || 1,
      spine: c.spine || "",
      chapterGoal: c.chapter_goal || "",
      summary: c.summary || "",
      scenes: (c.scenes || []).map(s => shapeScene(s)),
    })),
    unassigned: ((preview && preview.unassigned) || []).map(s => ({ ...shapeScene(s), anchored: false, planned: false })),
    warnings: (preview && preview.warnings) || [],
    rhythm: (preview && preview.rhythm) || null,
    rationale: (preview && preview.rationale) || "",
    scale: (preview && preview.scale) || null,
    chapterTable: (preview && preview.chapter_table) || null,
    replacesChapterCount: (preview && preview.replaces_chapter_count) || 0,
  };
}

/* 节奏体检 → 一组带标签的小项 { k, v }（k 为空就是一句结论）。只报结构本身看得出、作者一眼能行动的东西
   （这里还没有正文，谈「张力评分」是空话）。以前是一串「每章 2–5 场 · 均值 4.25」连着几段「第 1 幕：…」，
   读不出哪个数是什么。 */
export function rhythmSummary(rhythm) {
  if (!rhythm) return [];
  const items = [];
  if (rhythm.scene_counts && rhythm.scene_counts.length) {
    const lo = rhythm.min_scenes;
    const hi = rhythm.max_scenes;
    items.push({ k: "每章", v: lo === hi ? `${lo} 场` : `${lo}–${hi} 场` });
    const mean = Number(rhythm.mean_scenes_per_chapter);
    if (Number.isFinite(mean) && lo !== hi) items.push({ k: "平均", v: `${Math.round(mean * 10) / 10} 场` });
  }
  (rhythm.acts || []).forEach(a => {
    items.push({ k: ACT_LABEL[a.act] || `第 ${a.act} 幕`, v: `${a.chapter_count} 章 / ${a.scene_count} 场` });
  });
  const offHinge = (rhythm.spine_placement || []).filter(s => s.placed && !s.on_hinge).length;
  const missing = (rhythm.spine_placement || []).filter(s => !s.placed).length;
  if (!offHinge && !missing) items.push({ k: "", v: "三个灾难都落在幕的铰链上" });
  return items;
}

/* 章名框里已经带着这一章的章号吗：章名就是「第 N 章」这种占位，或者空着（占位提示「第 N 章（未命名）」里有章号）。
   这时章名框左边不再并排写一遍「第 N 章」。 */
export function chapterNoInTitle(title, index) {
  const text = String(title || "").trim();
  if (!text) return true;
  const m = /^第\s*(\d+)\s*章$/.exec(text);
  return !!(m && Number(m[1]) === index + 1);
}

/* 「每章约 N 场」这个数从哪来——面板必须说得出口，不能是个黑盒。 */
export function scaleExplanation(scale) {
  if (!scale) return "";
  const per = scale.scenes_per_chapter;
  const total = scale.scene_count || 0;
  const hint = scale.reference_hint || null;
  let base = "";
  if (scale.source === "reference" && hint) {
    const chars = Number(hint.chapter_chars_median) || 0;
    const wan = chars >= 10000 ? `${(chars / 10000).toFixed(1)} 万` : `${chars}`;
    base = `参考书一章约 ${wan}字 ≈ ${per} 场`;
  } else if (scale.source === "project_target") base = `作品设置：目标 ${scale.target_chapter_count} 章`;
  else if (scale.source === "request_target") base = `按你定的 ${scale.target_chapter_count} 章`;
  else if (scale.source === "request_per_chapter") base = `按你填的每章约 ${per} 场`;
  else base = `默认每章约 ${per} 场`;
  const byCount = scale.target_chapter_count > 0 ? scale.target_chapter_count : (per > 0 ? Math.ceil(total / per) : 0);
  const floor = scale.hinge_min_chapters || 0;
  if (floor && byCount < floor) base += ` · 三个灾难各自收束一章，所以至少 ${floor} 章`;
  return base;
}

/* 本地态 → materialize / chapter-plan 的提交载荷。
   replace_chapters：这就是整张章表——new:* 的章由后端新建，没列出来的旧章被软删。
   章内顺序由后端按场景列表的顺序重算，scene_seq 只是给旧调用方留的。 */
export function buildChapterPlanPayload(draft) {
  return {
    replace_chapters: true,
    chapters: draft.chapters.map(c => ({
      row_uid: c.rowUid,
      title: c.title,
      act: c.act,
      spine: c.spine,
      chapter_goal: c.chapterGoal,
      ...(c.summary != null ? { summary: c.summary } : {}),
    })),
    assignments: draft.chapters.flatMap(c =>
      c.scenes.map((s, si) => ({
        scene_plan_id: s.scenePlanId,
        chapter_row_uid: c.rowUid,
        scene_seq: si + 1,
      })),
    ),
  };
}

/* 章表 → 幕段（按**数组顺序**切，幕值一变就起新的一段）。

   刻意不按幕重新分组。重新分组会让「看到的顺序」和「保存的顺序」分家：draft.chapters
   可以是交错的，比如 [A(一幕), B(二幕), C(一幕)]。按幕分组的面板显示 第一幕: A、C ／ 第二幕: B，
   作者据此点了确认，而 buildChapterPlanPayload 交的是数组序 A、B、C，后端按数组序写 chapter_seq。
   按数组顺序切段则处处一致：显示 = 提交 = 落库。幕交错因此变成看得见的两段「第一幕」。 */
export function chapterActRuns(chapters) {
  const runs = [];
  (chapters || []).forEach((chapter, index) => {
    const act = chapter.act || 1;
    const last = runs[runs.length - 1];
    if (last && last.act === act) last.chapters.push({ ...chapter, index });
    else runs.push({ act, chapters: [{ ...chapter, index }] });
  });
  return runs;
}

const cloneDraft = (draft) => ({
  ...draft,
  chapters: draft.chapters.map(c => ({ ...c, scenes: c.scenes.slice() })),
  unassigned: draft.unassigned.slice(),
});

const AUTO_TITLE_RE = /^第\s*\d+\s*章$/;

/* 结构改动（挪章界 / 拆章 / 并章）之后的收口。
   章的灾难标记跟着场走：章里有带标记的场就取最后一个（灾难是章的收束点）；章上原有的标记如果
   属于一个已经搬去别章的场，就清掉；场上根本没有这个标记时，保留作者在 07 里的声明。 */
function respine(draft) {
  const marksOnScenes = new Set();
  draft.chapters.forEach(c => c.scenes.forEach(s => { if (s.spine) marksOnScenes.add(s.spine); }));
  draft.chapters.forEach((c, index) => {
    const inside = c.scenes.map(s => s.spine).filter(Boolean);
    if (inside.length) c.spine = inside[inside.length - 1];
    else if (c.spine && marksOnScenes.has(c.spine)) c.spine = "";
    /* 系统起的占位章名「第 N 章」跟着章序走（作者起的名字一个字不动）：拆章 / 并章之后
       「第 3 章」不能排在第 4 位，空章名落进目录会变成章 id 字符串。后端 save 同一条规则。 */
    const title = String(c.title || "").trim();
    if (!title || AUTO_TITLE_RE.test(title)) c.title = `第 ${index + 1} 章`;
  });
  return draft;
}

/* 挪章界。章是场景列表上连续的一段，所以只有两种合法的挪法：
   - 一章的**末场** → 下一章：它成为下一章的**第一场**（不是追加到末尾——那会把它排到后面几场之后）；
   - 一章的**首场** → 上一章：它成为上一章的最后一场。
   from = -1 是「未分配」区：按场景列表的序号插进目标章。其它组合一律不动（返回原对象）。 */
export function moveSceneToChapter(draft, from, sceneIndex, to) {
  if (from === to) return draft;
  if (from < 0) {
    if (!draft.chapters[to] || !draft.unassigned[sceneIndex]) return draft;
    const next = cloneDraft(draft);
    const [scene] = next.unassigned.splice(sceneIndex, 1);
    const target = next.chapters[to].scenes;
    const at = scene.storyIndex ? target.findIndex(s => s.storyIndex && s.storyIndex > scene.storyIndex) : -1;
    if (at < 0) target.push(scene); else target.splice(at, 0, scene);
    return respine(next);
  }
  const source = draft.chapters[from];
  if (!source || !source.scenes[sceneIndex]) return draft;
  if (to < 0) {
    const next = cloneDraft(draft);
    const [scene] = next.chapters[from].scenes.splice(sceneIndex, 1);
    next.unassigned.push(scene);
    return respine(next);
  }
  if (!draft.chapters[to]) return draft;
  const forward = to === from + 1 && sceneIndex === source.scenes.length - 1;
  const backward = to === from - 1 && sceneIndex === 0;
  if (!forward && !backward) return draft;
  const next = cloneDraft(draft);
  const [scene] = next.chapters[from].scenes.splice(sceneIndex, 1);
  if (forward) next.chapters[to].scenes.unshift(scene); else next.chapters[to].scenes.push(scene);
  return respine(next);
}

/* 从某一场另起一章：这一场和它后面的场搬进紧跟着的一章新章（临时身份 new:*，确认时后端铸真身）。 */
export function splitChapterAt(draft, chapterIndex, sceneIndex) {
  const source = draft.chapters[chapterIndex];
  if (!source || sceneIndex <= 0 || sceneIndex >= source.scenes.length) return draft;
  const next = cloneDraft(draft);
  const moved = next.chapters[chapterIndex].scenes.splice(sceneIndex);
  next.chapters.splice(chapterIndex + 1, 0, {
    rowUid: mintChapterKey(), title: "", act: source.act || 1, spine: "", chapterGoal: "", summary: "", scenes: moved,
  });
  return respine(next);
}

/* 与上一章合并：这一章的场接在上一章后面，这一章从章表里拿掉（确认时后端软删它）。 */
export function mergeChapterIntoPrevious(draft, chapterIndex) {
  if (chapterIndex <= 0 || !draft.chapters[chapterIndex]) return draft;
  const next = cloneDraft(draft);
  const [gone] = next.chapters.splice(chapterIndex, 1);
  next.chapters[chapterIndex - 1].scenes.push(...gone.scenes);
  return respine(next);
}

/* 章名是不是系统起的占位（空 / 「第 N 章」/「（待补）」）——AI 起章名只碰这些。与后端 is_auto_chapter_title 同一口径。 */
export function isAutoChapterTitle(title) {
  const text = String(title || "").trim();
  return !text || AUTO_TITLE_RE.test(text) || ["待补", "TODO", "todo", "TBD", "tbd", "占位"].some(m => text.includes(m));
}

/* 章摘要是不是从场上抄来的默认值（空，或与本面板里某一场的摘要一字不差）——这种摘要可以被 AI 写的替掉；
   作者自己写的不动。 */
function isAutoChapterSummary(draft, chapter) {
  const text = String(chapter.summary || "").trim();
  if (!text) return true;
  return draft.chapters.some(c => c.scenes.some(s => String(s.summary || "").trim() === text));
}

/* AI 起章名的请求载荷：面板此刻的章表（含还没确认的 new:* 章与手调过的归属）。 */
export function buildChapterTitlesRequest(draft) {
  return draft.chapters.filter(c => c.scenes.length).map(c => ({
    row_uid: c.rowUid, title: c.title, act: c.act, spine: c.spine,
    scene_plan_ids: c.scenes.map(sc => sc.scenePlanId),
  }));
}

/* 把 AI 起的章名放进面板。只替系统起的占位名；章摘要同理只替从场上抄来的默认值——作者写过的一个字不动。
   返回 { draft, applied }；没有任何一章被改时返回原 draft 对象。 */
export function applyChapterNames(draft, titles, options) {
  const renameAll = !!(options && options.renameAll);
  const byUid = {};
  (titles || []).forEach(t => { if (t && t.row_uid && String(t.title || "").trim()) byUid[t.row_uid] = t; });
  let applied = 0;
  const chapters = draft.chapters.map(c => {
    const hit = byUid[c.rowUid];
    if (!hit || !(renameAll || isAutoChapterTitle(c.title))) return c;
    applied += 1;
    const summary = String(hit.summary || "").trim();
    return { ...c, title: String(hit.title).trim(), ...(summary && isAutoChapterSummary(draft, c) ? { summary } : {}) };
  });
  return applied ? { draft: { ...draft, chapters }, applied } : { draft, applied: 0 };
}

/* 未分配的一场该回哪一章：场景列表里排在它前面的最后一场所在的章（没有就第一章）。 */
export function homeChapterFor(draft, scene) {
  if (!draft.chapters.length) return -1;
  let home = 0;
  draft.chapters.forEach((c, ci) => {
    if (c.scenes.some(s => s.storyIndex && scene.storyIndex && s.storyIndex < scene.storyIndex)) home = ci;
  });
  return home;
}
