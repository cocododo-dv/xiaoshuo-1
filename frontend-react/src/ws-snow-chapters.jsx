import React from "react";
import { I } from "./icons.jsx";

/* global window */
/* ==========================================================
   分章预览面板（P2；2026-09-18 重做）
   ----------------------------------------------------------
   「整理为章节结构」先开这个面板 —— 作者按下确认之前就看得见会得到什么：哪一章拿到
   哪几场、哪些场还没分到、哪些章是空的。确认之前什么都不落库。

   这一版的纪律（对应一次真实的「整理出来乱七八糟」）：
   - **章是场景列表上连续的一段**。场的先后只有一个来源——09 场景列表；面板不提供第二套
     「章内手排」，只提供挪章界（首场并入上一章 / 末场移到下一章）、从某一场另起一章、与上一章合并。
   - 每一场带着它在场景列表里的序号和功能标签，作者一眼看得出顺序对不对。
   - 打开时由服务端按现状挑方案（auto）：分过章就原样摆出来；07 里有作者写的章表就把场倒进去；
     什么都没有（或只有几行「（待补）」）就直接按场景分章——三个灾难各自收束一章。
   - 「每章约 N 场」是面板上的一个数，并写明这个数从哪来（参考书章长 / 作品设置 / 默认）。

   分章算法在后端（snowflake_chaptering.py），前端不持有第二套。
   ========================================================== */

const STRATEGIES = [
  { key: "from_scenes", label: "按场景分章", hint: "章是列完场之后的包装决定：三个灾难各自收束一章，其余按「每章约 N 场」顺着场景列表切开" },
  { key: "spine_anchor", label: "倒进 07 章表", needsTable: true, hint: "用 07 里你写的章表：同一个灾难标记的场与章互相锁定，其余按顺序铺开" },
  { key: "even", label: "07 章表 · 均分", needsTable: true, hint: "用 07 里你写的章表：忽略灾难标记，按顺序把场平均分进各章" },
  { key: "keep_current", label: "已保存的分章", needsSaved: true, hint: "上一次确认 / 保存的分章原样摆出来，新加的场跟着它前一场走" },
];

const ACT_LABEL = { 1: "第一幕", 2: "第二幕", 3: "第三幕" };
const NEW_PREFIX = "new:";
let newChapterCounter = 0;
const mintChapterKey = () => `${NEW_PREFIX}fe${Date.now().toString(36)}${(newChapterCounter++).toString(36)}`;
const isNewChapter = (chapter) => String((chapter && chapter.rowUid) || "").startsWith(NEW_PREFIX);

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
function shapeDraft(preview) {
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

/* 节奏体检 → 一行人话。只报结构本身看得出、作者一眼能行动的东西
   （这里还没有正文，谈「张力评分」是空话）。 */
export function rhythmSummary(rhythm) {
  if (!rhythm) return [];
  const lines = [];
  if (rhythm.scene_counts && rhythm.scene_counts.length) {
    lines.push(`每章 ${rhythm.min_scenes}–${rhythm.max_scenes} 场 · 均值 ${rhythm.mean_scenes_per_chapter}`);
  }
  (rhythm.acts || []).forEach(a => {
    lines.push(`第 ${a.act} 幕：${a.chapter_count} 章 / ${a.scene_count} 场`);
  });
  const offHinge = (rhythm.spine_placement || []).filter(s => s.placed && !s.on_hinge).length;
  const missing = (rhythm.spine_placement || []).filter(s => !s.placed).length;
  if (!offHinge && !missing) lines.push("三个灾难都落在幕的铰链上");
  return lines;
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

/* 这张面板有两扇门：构思页头的「整理为章节结构」，和章节编排里的「整理章节结构」（阶段 Z）。章的结构只有
   这一个编辑器；onGoToScene(sceneId) 让面板里的一场直达构思第 10 步的那一场（由宿主视图决定怎么跳）。 */
export function WsChapterPlanPanel({ onClose, onDone, onGoToStep, onGoToScene }) {
  const [busy, setBusy] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const [draft, setDraft] = React.useState(null);
  const [dirty, setDirty] = React.useState(false);
  const [perChapter, setPerChapter] = React.useState("");
  const [materializationGate, setMaterializationGate] = React.useState(null);
  const previewRequestRef = React.useRef(null);

  const load = React.useCallback((strategy, options) => {
    const signature = `${strategy}|${JSON.stringify(options || {})}`;
    const inflight = previewRequestRef.current;
    if (inflight && inflight.signature === signature) return inflight.promise;
    const promise = (async () => {
      setBusy(true);
      setError("");
      try {
        if (!window.SnowSync || typeof window.SnowSync.chapterPreview !== "function") {
          throw new Error("雪花同步模块尚未就绪，请刷新页面后重试。");
        }
        const preview = await window.SnowSync.chapterPreview(strategy, options || {});
        const shaped = shapeDraft(preview);
        setDraft(shaped);
        setDirty(false);
        if (shaped.scale && shaped.scale.scenes_per_chapter) setPerChapter(String(shaped.scale.scenes_per_chapter));
        setMaterializationGate((preview && preview.materialization_gate) || null);
      } catch (e) {
        setError((e && e.message) || "无法生成分章预览，请稍后重试。");
        setDraft(null);
        setMaterializationGate(null);
      } finally {
        setBusy(false);
      }
    })();
    previewRequestRef.current = { signature, promise };
    promise.finally(() => {
      if (previewRequestRef.current && previewRequestRef.current.promise === promise) {
        previewRequestRef.current = null;
      }
    });
    return promise;
  }, []);

  /* 换方案会丢掉面板里还没确认的手动调整——先问一句。 */
  const switchTo = (strategy, options) => {
    if (busy || saving) return;
    if (dirty && !window.confirm("换一种分法会丢掉你在面板里还没确认的调整（挪章界 / 拆章 / 并章 / 改章名）。继续？")) return;
    load(strategy, options);
  };
  const applyScale = () => {
    const n = Math.round(Number(perChapter));
    if (!(n > 0)) return;
    switchTo("from_scenes", { scenesPerChapter: n });
  };

  /* 处置孤儿场（作者从 09 删掉、但目录里已有场景卡的那些场）。处置完必须重拉预览：
     孤儿警告是后端算的，本地删掉那一条会让界面和真相分家。 */
  const [resolving, setResolving] = React.useState("");
  const resolveOrphan = async (scenePlanId, action) => {
    setResolving(scenePlanId);
    setError("");
    try {
      await window.SnowSync.resolveOrphanedScene(scenePlanId, action);
      await load(draft ? draft.strategy : "auto");
    } catch (e) {
      setError((e && e.message) || "处置这一场失败，请稍后重试。");
    } finally {
      setResolving("");
    }
  };

  /* 让 AI 建议分章（P3）：结果是另一份候选预览，采纳与否只是本地状态。
     fail-closed —— LLM 没配好就如实报错，不拿规则结果冒充 AI 建议。
     AI 是往**已经存在**的章里分场；面板里还没确认的新章（new:*）它看不见，所以那时不可用。 */
  const [suggesting, setSuggesting] = React.useState(false);
  const hasUnsavedChapters = !!draft && draft.chapters.some(isNewChapter);
  const suggest = async () => {
    if (suggesting || busy || saving || hasUnsavedChapters) return;
    if (dirty && !window.confirm("AI 建议会替换面板里还没确认的调整。继续？")) return;
    setSuggesting(true);
    setError("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.chapterSuggest !== "function") {
        throw new Error("AI 分章能力尚未就绪，请刷新页面后重试。");
      }
      const base = draft && draft.strategy !== "llm_suggested" && draft.strategy !== "from_scenes" ? draft.strategy : "keep_current";
      const suggestion = await window.SnowSync.chapterSuggest(base);
      setDraft(shapeDraft(suggestion));
      setDirty(false);
      setMaterializationGate((suggestion && suggestion.materialization_gate) || null);
    } catch (e) {
      setError((e && e.message) || "AI 分章建议不可用，请检查模型配置后重试。");
    } finally {
      setSuggesting(false);
    }
  };

  /* AI 起章名（阶段 W）：只给系统起的占位名起名，结果回到面板里由你改、由你确认；不落库。
     fail-closed —— LLM 没配好就如实报错。面板里还没确认的新章也能起（请求带的是面板此刻的章表）。 */
  const [naming, setNaming] = React.useState(false);
  const [nameNote, setNameNote] = React.useState("");
  const draftRef = React.useRef(null);
  draftRef.current = draft;
  const unnamedCount = draft ? draft.chapters.filter(c => c.scenes.length && isAutoChapterTitle(c.title)).length : 0;
  const nameChapters = async () => {
    if (naming || busy || saving || !draft || !unnamedCount) return;
    setNaming(true);
    setError("");
    setNameNote("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.chapterTitles !== "function") {
        throw new Error("AI 起章名尚未就绪，请刷新页面后重试。");
      }
      const result = await window.SnowSync.chapterTitles(buildChapterTitlesRequest(draft));
      // 等模型的这段时间里作者可能还在改章名：基于**此刻**的面板算，而不是点按钮时的那一份
      const { draft: next, applied } = applyChapterNames(draftRef.current || draft, (result && result.titles) || []);
      if (applied) { setDraft(next); setDirty(true); }
      const notice = result && result.notice && result.notice.message;
      setNameNote(notice || (applied ? `AI 起了 ${applied} 个章名 —— 可以直接改，确认写入时一起落库。` : "这一次没有起出新的章名。"));
    } catch (e) {
      setError((e && e.message) || "AI 起章名不可用，请检查模型配置后重试。");
    } finally {
      setNaming(false);
    }
  };

  React.useEffect(() => { load("auto"); }, [load]);

  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && !saving) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [saving, onClose]);

  const blockers = ((draft && draft.warnings) || []).filter(w => w.severity === "blocker");
  /* 手动调整之后，后端算的「顺序冲突 / 空章 / 过大章」可能已经不成立——只保留与归属无关的提醒 */
  const STRUCTURAL_KINDS = ["chapter_order_conflict", "empty_chapter", "oversized_chapter", "unassigned_scenes", "spine_not_placed", "spine_off_hinge"];
  const advisories = ((draft && draft.warnings) || []).filter(w => w.severity !== "blocker"
    && !(dirty && STRUCTURAL_KINDS.includes(w.kind)));
  const sceneTotal = draft ? draft.chapters.reduce((n, c) => n + c.scenes.length, 0) : 0;
  const chapterTotal = draft ? draft.chapters.filter(c => c.scenes.length).length : 0;
  const rawGateItems = Array.isArray(materializationGate && materializationGate.items)
    ? materializationGate.items
    : [];
  /* workspace 的当前真相在确认前必然可能含 chapter_plan_required；当前预览已经提供完整
     chapters + assignments 时，这一项会在 materialize 同一事务先被满足，不能反过来把
     “确认分章”按钮锁死。其他步骤/分诊阻断仍必须提前展示。 */
  const gateItems = rawGateItems.filter(item => !(
    item && item.kind === "chapter_plan_required"
    && draft && !draft.unassigned.length && chapterTotal > 0 && sceneTotal > 0
  ));
  const gateBlockers = gateItems.filter(item => item && item.severity === "blocker");
  const gateAdvisories = gateItems.filter(item => item && item.severity !== "blocker");
  const canConfirm = !!draft && !busy && !saving && !blockers.length && !gateBlockers.length
    && !draft.unassigned.length && sceneTotal > 0;

  const goToGateItem = (item) => {
    const stepKey = (item && item.step_key)
      || (item && item.primary_action && item.primary_action.step_key);
    if (!stepKey || typeof onGoToStep !== "function") return;
    onGoToStep(stepKey);
    onClose();
  };

  const confirm = async () => {
    if (!canConfirm) return;
    setSaving(true);
    setError("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.materialize !== "function") {
        throw new Error("雪花同步模块尚未就绪，请刷新页面后重试。");
      }
      const payload = buildChapterPlanPayload({ ...draft, chapters: draft.chapters.filter(c => c.scenes.length || !isNewChapter(c)) });
      const result = await window.SnowSync.materialize(null, payload);
      onDone(result);
    } catch (e) {
      const freshGate = e && e.details && e.details.materialization_gate;
      if (freshGate) {
        setMaterializationGate(freshGate);
        setError("还有整理前检查没有通过；请按下面的阻断项处理后重试。");
      } else {
        setError((e && e.message) || "写入章节结构失败，请稍后重试。");
      }
      setSaving(false);
    }
  };

  const edit = (fn) => { setDraft(d => fn(d)); setDirty(true); };
  const move = (from, sceneIndex, to) => edit(d => moveSceneToChapter(d, from, sceneIndex, to));
  const split = (chapterIndex, sceneIndex) => edit(d => splitChapterAt(d, chapterIndex, sceneIndex));
  const merge = (chapterIndex) => edit(d => mergeChapterIntoPrevious(d, chapterIndex));
  const setChapter = (index, field, value) =>
    edit(d => ({ ...d, chapters: d.chapters.map((c, i) => (i === index ? { ...c, [field]: value } : c)) }));

  const actRuns = chapterActRuns(draft ? draft.chapters : []);
  const table = (draft && draft.chapterTable) || null;
  const strategyDisabled = (s) => busy || saving
    || (s.needsTable && table && !table.authored)
    || (s.needsSaved && table && !table.saved);
  const scaleNote = draft && draft.strategy === "from_scenes" ? scaleExplanation(draft.scale) : "";
  const sceneText = (scene) => (scene.title && scene.title !== scene.summary ? scene.title : (scene.summary || scene.title));
  /* 去改这一场：面板里还没确认的调整会丢，先问一句 */
  const goToScene = (scene) => {
    if (typeof onGoToScene !== "function" || !scene.sceneId || saving) return;
    if (dirty && !window.confirm("去构思里改这一场会关掉面板，面板里还没确认的调整（挪章界 / 拆章 / 并章 / 改章名）不会保留。继续？")) return;
    onGoToScene(scene.sceneId);
  };

  return (
    <div className="sf-sd-scrim" role="dialog" aria-modal="true" aria-label="整理为章节结构" onClick={saving ? undefined : onClose}>
      <div className="sf-sd-card sf-chapterplan" data-testid="chapter-plan-panel" onClick={(e) => e.stopPropagation()}>
        <header className="sf-sd-head">
          <div>
            <div className="sf-sd-sub">整理为章节结构 · 预览</div>
            <div className="sf-sd-title">确认之前，先看清每一场归哪一章</div>
          </div>
          <button className="wr-drawer-x" onClick={onClose} disabled={saving} title="关闭 (Esc)" aria-label="关闭">
            <I.X size={16} />
          </button>
        </header>

        <div className="sf-chapterplan-bar">
          <span className="text-muted text-sm">怎么分</span>
          {STRATEGIES.map(s => (
            <button
              key={s.key}
              className={`btn btn-sm ${draft && draft.strategy === s.key ? "btn-accent" : "btn-quiet"}`}
              title={s.needsTable && table && !table.authored ? "07 里还没有你写的章表（只有占位行或空着）——按场景分章就好" : s.hint}
              disabled={strategyDisabled(s)}
              onClick={() => switchTo(s.key)}
              data-testid={`chapter-plan-strategy-${s.key}`}
            >
              {s.label}
            </button>
          ))}
          <button className="btn btn-quiet btn-sm" disabled={busy || saving || suggesting || hasUnsavedChapters}
            title={hasUnsavedChapters ? "AI 是往已经确认的章里分场——先确认写入这一版章表，再让它建议" : "让 AI 依据灾难标记与上下游材料给一份分章建议；采纳与否由你决定"}
            onClick={suggest} data-testid="chapter-plan-suggest">
            <I.Wand size={13} className={suggesting ? "sf-spin" : ""} /> {suggesting ? "推演中…" : "AI 建议"}
          </button>
          <button className="btn btn-quiet btn-sm" disabled={busy || saving || naming || !unnamedCount}
            title={unnamedCount
              ? `给还叫「第 N 章」的 ${unnamedCount} 章各起一个名字、写一句章摘要；你起过名字的章不动。结果可以直接改，确认写入时才落库`
              : "每一章都已经有你起的名字了；想让 AI 重起某一章，先把它的章名清空"}
            onClick={nameChapters} data-testid="chapter-plan-name">
            <I.Tag size={13} className={naming ? "sf-spin" : ""} /> {naming ? "起名中…" : "AI 起章名"}
          </button>
          <span style={{ flex: 1 }} />
          {draft && <span className="text-muted text-sm">{chapterTotal} 章 · {sceneTotal} 场</span>}
        </div>

        {!busy && draft && nameNote && (
          <div className="sf-chapterplan-rationale" data-testid="chapter-plan-name-note">
            <I.Tag size={12} /> <span>{nameNote}</span>
          </div>
        )}

        {!busy && draft && draft.strategy === "from_scenes" && (
          <div className="sf-chapterplan-scale" data-testid="chapter-plan-scale">
            <label className="sf-chapterplan-scale-field">
              每章约
              <input type="number" min="1" max="99" value={perChapter} disabled={saving}
                onChange={e => setPerChapter(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") applyScale(); }}
                aria-label="每章约几场" data-testid="chapter-plan-per-chapter" />
              场
            </label>
            <button className="btn btn-quiet btn-xs" disabled={busy || saving || !(Number(perChapter) > 0)}
              onClick={applyScale} data-testid="chapter-plan-rescale">
              <I.Refresh size={11} /> 重新分
            </button>
            {scaleNote && <span className="sf-chapterplan-scale-note">{scaleNote}</span>}
            {!!draft.replacesChapterCount && (
              <span className="sf-chapterplan-scale-note">确认后替换现有的 {draft.replacesChapterCount} 章章表（07 的章节表随之更新）</span>
            )}
          </div>
        )}

        {!busy && draft && draft.rhythm && !dirty && (
          <div className="sf-chapterplan-rhythm" data-testid="chapter-plan-rhythm">
            <I.Activity size={12} />
            {rhythmSummary(draft.rhythm).map((line, i) => <span key={i} className="sf-chapterplan-rhythmitem">{line}</span>)}
          </div>
        )}

        {!busy && draft && draft.rationale && (
          <div className="sf-chapterplan-rationale" data-testid="chapter-plan-rationale">
            <I.Wand size={12} /> <span>{draft.rationale}</span>
          </div>
        )}

        {busy && <div className="sf-chapterplan-empty">正在推演分章…</div>}
        {!busy && error && !draft && <div className="sf-chapterplan-empty tone-rose" role="alert">{error}</div>}

        {!busy && draft && (
          <>
            <div className="sf-chapterplan-body">
              {actRuns.map(group => (
                <div key={`act-${group.act}-${group.chapters[0].index}`} className="sf-chapterplan-act">
                  <div className="sf-chapterplan-actlabel">{ACT_LABEL[group.act] || `第 ${group.act} 幕`}</div>
                  {group.chapters.map(chapter => {
                    const first = chapter.scenes[0];
                    const last = chapter.scenes[chapter.scenes.length - 1];
                    const range = first && first.storyIndex
                      ? (first === last ? `第 ${first.storyIndex} 场` : `第 ${first.storyIndex}–${last.storyIndex} 场`)
                      : "";
                    return (
                      <div key={chapter.rowUid} className={`sf-chapterplan-chapter ${chapter.scenes.length ? "" : "is-empty"}`}
                        data-testid={`chapter-plan-chapter-${chapter.index}`}>
                        <div className="sf-chapterplan-chaphead">
                          <span className="sf-chapterplan-chapno">{String(chapter.index + 1).padStart(2, "0")}</span>
                          <input
                            className="sf-chapterplan-title"
                            value={chapter.title}
                            placeholder={`第 ${chapter.index + 1} 章（未命名）`}
                            onChange={e => setChapter(chapter.index, "title", e.target.value)}
                            aria-label={`第 ${chapter.index + 1} 章标题`}
                          />
                          {chapter.spine && <span className="sf-chapterplan-spine" title="这一章收束在这个灾难上">{chapter.spine}</span>}
                          <span className="sf-chapterplan-count">{chapter.scenes.length} 场{range ? ` · ${range}` : ""}</span>
                          {chapter.index > 0 && (
                            <button className="btn btn-ghost btn-xs" disabled={saving}
                              title="与上一章合并：这一章的场接到上一章后面" aria-label="与上一章合并"
                              data-testid={`chapter-plan-merge-${chapter.index}`}
                              onClick={() => merge(chapter.index)}>
                              <I.ChevronUp size={12} /> 并入上一章
                            </button>
                          )}
                        </div>
                        {!!chapter.scenes.length && (
                          <input
                            className="sf-chapterplan-sum"
                            value={chapter.summary || ""}
                            placeholder="这一章把局面推到哪——一句话（留空就取章末那一场）"
                            onChange={e => setChapter(chapter.index, "summary", e.target.value)}
                            aria-label={`第 ${chapter.index + 1} 章摘要`}
                            data-testid={`chapter-plan-summary-${chapter.index}`}
                          />
                        )}
                        <ul className="sf-chapterplan-scenes">
                          {chapter.scenes.map((scene, si) => {
                            const isFirst = si === 0;
                            const isLast = si === chapter.scenes.length - 1;
                            return (
                              <li key={scene.scenePlanId} className="sf-chapterplan-scene">
                                <span className="sf-chapterplan-no">{scene.storyIndex ? String(scene.storyIndex).padStart(2, "0") : "··"}</span>
                                <span className={`sf-chapterplan-kind is-${scene.primaryForm}`}>
                                  {scene.primaryForm === "reactive" ? "反应" : "主动"}
                                </span>
                                <span className="sf-chapterplan-scenemain">
                                  {scene.fn && <span className="sf-chapterplan-fn">{scene.fn}</span>}
                                  <span className="sf-chapterplan-scenetitle" title={scene.summary || scene.title}>{sceneText(scene)}</span>
                                </span>
                                {scene.spine && <span className="sf-chapterplan-anchor" title="灾难场：它收束所在的章">●{scene.spine}</span>}
                                {!scene.planned && <span className="sf-chapterplan-unplanned" title="第 10 步还没规划三拍">未规划</span>}
                                <span className="sf-chapterplan-sceneacts">
                                  {typeof onGoToScene === "function" && scene.sceneId && (
                                    <button className="btn btn-ghost btn-xs" disabled={saving}
                                      title="去构思第 10 步改这一场的设计（形态 / 三拍 / POV）" aria-label="在构思里改这一场"
                                      data-testid={`chapter-plan-scene-edit-${scene.storyIndex || si}`}
                                      onClick={() => goToScene(scene)}>
                                      <I.Pen size={12} />
                                    </button>
                                  )}
                                  {isFirst && chapter.index > 0 && (
                                    <button className="btn btn-ghost btn-xs" disabled={saving}
                                      title="这一章的第一场并入上一章（成为上一章的最后一场）" aria-label="并入上一章"
                                      onClick={() => move(chapter.index, si, chapter.index - 1)}>
                                      <I.ChevronUp size={12} />
                                    </button>
                                  )}
                                  {!isFirst && (
                                    <button className="btn btn-ghost btn-xs" disabled={saving}
                                      title="从这一场另起一章：它和后面的场搬进一章新章" aria-label="从这里另起一章"
                                      data-testid={`chapter-plan-split-${chapter.index}-${si}`}
                                      onClick={() => split(chapter.index, si)}>
                                      <I.Scissors size={12} />
                                    </button>
                                  )}
                                  {isLast && chapter.index < draft.chapters.length - 1 && (
                                    <button className="btn btn-ghost btn-xs" disabled={saving}
                                      title="这一章的最后一场移到下一章（成为下一章的第一场）" aria-label="移到下一章"
                                      onClick={() => move(chapter.index, si, chapter.index + 1)}>
                                      <I.ChevronDown size={12} />
                                    </button>
                                  )}
                                </span>
                              </li>
                            );
                          })}
                          {!chapter.scenes.length && <li className="sf-chapterplan-scene is-placeholder">（没有分到场 · 不会写入目录）</li>}
                        </ul>
                      </div>
                    );
                  })}
                </div>
              ))}

              {!!draft.unassigned.length && (
                <div className="sf-chapterplan-chapter is-unassigned">
                  <div className="sf-chapterplan-chaphead">
                    <span className="sf-chapterplan-title as-text">未分配</span>
                    <span className="sf-chapterplan-count">{draft.unassigned.length} 场</span>
                  </div>
                  <ul className="sf-chapterplan-scenes">
                    {draft.unassigned.map((scene, si) => (
                      <li key={scene.scenePlanId} className="sf-chapterplan-scene">
                        <span className="sf-chapterplan-no">{scene.storyIndex ? String(scene.storyIndex).padStart(2, "0") : "··"}</span>
                        <span className="sf-chapterplan-scenemain">
                          {scene.fn && <span className="sf-chapterplan-fn">{scene.fn}</span>}
                          <span className="sf-chapterplan-scenetitle" title={scene.summary || scene.title}>{sceneText(scene)}</span>
                        </span>
                        <button className="btn btn-quiet btn-xs" disabled={!draft.chapters.length}
                          title="归入场景列表里它前一场所在的章"
                          onClick={() => move(-1, si, homeChapterFor(draft, scene))}>归入它前一场的章</button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            {(blockers.length || advisories.length || gateItems.length || draft.unassigned.length) ? (
              <div className="sf-chapterplan-warnings" data-testid="chapter-plan-warnings">
                {blockers.map((w, i) => (
                  <div key={`b${i}`} className="sf-chapterplan-warn tone-rose">
                    <I.AlertTriangle size={12} /> {w.message}
                    {/* 孤儿场的 blocker 明说「请先决定是一并删除还是保留」——那两个决定
                        必须真的在这里，否则这条警告就是个无解的死结。 */}
                    {w.kind === "orphaned_scene" && w.scene_plan_id ? (
                      <span className="sf-chapterplan-warn-actions">
                        <button className="btn btn-quiet btn-xs" disabled={!!resolving}
                          onClick={() => resolveOrphan(w.scene_plan_id, "keep")}>
                          保留正文
                        </button>
                        <button className="btn btn-quiet btn-xs" disabled={!!resolving}
                          onClick={() => resolveOrphan(w.scene_plan_id, "discard")}>
                          一并删除
                        </button>
                      </span>
                    ) : null}
                  </div>
                ))}
                {!!draft.unassigned.length && (
                  <div className="sf-chapterplan-warn tone-rose">
                    <I.AlertTriangle size={12} /> 还有 {draft.unassigned.length} 场没有分到章 —— 指派完才能写入。
                  </div>
                )}
                {advisories.map((w, i) => (
                  <div key={`w${i}`} className="sf-chapterplan-warn tone-gold">
                    <I.AlertTriangle size={12} /> {w.message}
                    {w.kind === "chapter_order_conflict" ? (
                      <span className="sf-chapterplan-warn-actions">
                        <button className="btn btn-quiet btn-xs" disabled={busy || saving}
                          data-testid="chapter-plan-fix-order"
                          onClick={() => switchTo("from_scenes")}>
                          按场景重新分章
                        </button>
                      </span>
                    ) : null}
                  </div>
                ))}
                {gateBlockers.map((item, i) => (
                  <div key={item.id || `gb${i}`} className="sf-chapterplan-warn tone-rose" role="alert"
                    data-testid="materialization-gate-blocker">
                    <I.AlertTriangle size={12} /> {item.message}
                    {typeof onGoToStep === "function" && item.step_key ? (
                      <span className="sf-chapterplan-warn-actions">
                        <button className="btn btn-quiet btn-xs" onClick={() => goToGateItem(item)}>
                          {(item.primary_action && item.primary_action.label) || "去补这一步"}
                        </button>
                      </span>
                    ) : null}
                  </div>
                ))}
                {gateAdvisories.map((item, i) => (
                  <div key={item.id || `gw${i}`} className="sf-chapterplan-warn tone-gold">
                    <I.AlertTriangle size={12} /> {item.message}
                  </div>
                ))}
              </div>
            ) : null}

            {error && <div className="sf-chapterplan-warn tone-rose" role="alert">{error}</div>}

            <footer className="sf-sd-foot">
              <button className="btn btn-ghost btn-sm" onClick={onClose} disabled={saving}>取消</button>
              <button className="btn btn-accent btn-sm" onClick={confirm} disabled={!canConfirm}
                data-testid="chapter-plan-confirm">
                {saving ? "写入中…" : `确认写入 ${chapterTotal} 章 / ${sceneTotal} 场`}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  );
}
