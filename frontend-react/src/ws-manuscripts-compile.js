import { escapeManuscriptText } from "./manuscript-html.js";
import { chapterLabel, chapterStage, chapterStateMeta, dramaFieldLabel } from "./labels/catalog.js";

/* ==========================================================
   ws-manuscripts-compile — 成稿中心的纯函数
   ----------------------------------------------------------
   · 服务端权威稿的判定（正文齐没齐、能不能批准，卡在哪一步）；正史核对不在其中（可选，不拦批准与导出）；
   · 目录章 + 服务端聚合快照 → 阅读器用的逐场正文；
   · 目录 → 左栏的章行、分组与整书进度格；
   · 导出：把一个范围内的章编译成 Markdown / 纯文本 / Word 兼容 HTML（没写齐的章照样导出，缺的场留一行占位）。
   快照一律由调用方传入（通常是 WsManuStore.snapshot 的结果），这里不读 store、
   不碰 DOM、不写 window——所以能直接单测。
   ========================================================== */

/* 没有后端 id / 还没拉过的章：没有正文可言（工作流与导出都用这一份） */
export const MANU_IDLE_SNAPSHOT = Object.freeze({ status: "idle", body: null, error: null });

/* 服务端说这一章的正文齐了、可以批准：聚合完整、没有缺场。
   正史核对不在其中（2026-10 起）：没核对的场照样能批准、导出（故事记忆照样用它抽出来的事实，驳回的除外）。 */
export function manuCanonicalComplete(snapshot) {
  const body = snapshot && snapshot.body;
  return !!(
    snapshot && snapshot.status === "ready" && body
    && body.completion === "complete"
    && Array.isArray(body.missingSceneIds)
    && body.missingSceneIds.length === 0
  );
}

/* 还不能批准时，一句话说清卡在哪。 */
export function manuCanonicalBlockReason(snapshot) {
  if (!snapshot || snapshot.status === "idle") return "正在等待服务端正文核验。";
  if (snapshot.status === "loading") return "正在从服务端加载权威正文。";
  if (snapshot.status === "error") return (snapshot.error && snapshot.error.message) || "服务端正文加载失败，请重试。";
  const missing = (snapshot.body && snapshot.body.missingSceneIds) || [];
  if (missing.length) return `服务端仍缺 ${missing.length} 场归档正文。`;
  return "服务端尚未将本章标记为可流转稿。";
}

/* 按章依次定稿：只有项目的当前章（目录的 current = 后端 project.current_chapter_id）能批准，别的章服务端一律 409
   PROJECT_CHAPTER_NOT_CURRENT。轮到这一章就是空串；没轮到就说先批哪一章——写齐了的后一章照样读作「等你批准」，
   但「批准为终稿」不该邀请一次注定失败的批准。 */
export function manuApprovalTurnReason(chapter, chapters) {
  if (!chapter || chapter.current) return "";
  const current = (chapters || []).find((c) => c && c.current);
  return current ? `按章依次定稿：先批准${chapterLabel(current, { withTitle: false })}。` : "按章依次定稿：这一章还没轮到。";
}

/* 有终稿、正史还没核对的场数——批准对话框里一句不拦的提示（服务端没给正史状态就是 0）。 */
export function manuCanonPendingCount(snapshot) {
  const canon = snapshot && snapshot.status === "ready" && snapshot.body && snapshot.body.canonContinuity;
  return canon && Array.isArray(canon.pending_scene_ids) ? canon.pending_scene_ids.length : 0;
}

/* 成稿中心看的章的说明两格（键与叫法同 labels/catalog.js 的 DRAMA_FIELDS：结构页签的概要卡、导出的附录都按这个顺序；
   备注只给作者自己看，不进成稿） */
export const MANU_DRAMA_KEYS = ["promise", "forbidden"];

/* 目录章的说明 → 这两格（键就是后端的键）；两格都空就没有。 */
export function manuDramaOf(chapter) {
  const d = (chapter && chapter.drama) || {};
  const pick = (v) => (v && v !== "—" ? v : "");
  if (MANU_DRAMA_KEYS.every((key) => !pick(d[key]))) return null;
  return Object.fromEntries(MANU_DRAMA_KEYS.map((key) => [key, pick(d[key])]));
}

/* 某一场的归档段落（服务端有终稿才有），没有就是 null。 */
export function manuArchivedParas(snapshot, scene) {
  const body = snapshot && snapshot.status === "ready" ? snapshot.body : null;
  if (!body || !scene || !scene.backendId) return null;
  const hit = (body.scenes || []).find((entry) => entry.sceneId === scene.backendId);
  return hit && hit.live && hit.paras && hit.paras.length ? hit.paras : null;
}

/* 只认服务端权威稿：没有 ready 快照就没有正文（加载失败也不退回任何示例稿）。
   有目录场景时按目录顺序排，缺终稿的场留占位；目录没有场景时按服务端归档行排。 */
export function manuBuildBody(chapter, snapshot) {
  if (!chapter || !chapter.backendId) return null;
  if (!snapshot || snapshot.status !== "ready" || !snapshot.body) return null;
  const ms = snapshot.body;
  const archived = ms.scenes || [];
  const catalogScenes = chapter.scenes || [];
  const catalogShape = catalogScenes.length > 0;
  const missingIds = new Set(ms.missingSceneIds || []);
  const source = catalogShape ? catalogScenes : archived;
  const scenes = source.map((scene, i) => {
    const sceneId = catalogShape ? scene.backendId : scene.sceneId;
    const hit = catalogShape
      ? (sceneId ? archived.find((entry) => entry.sceneId === sceneId) : archived[i])
      : scene;
    const live = !!(hit && hit.live && hit.paras && hit.paras.length);
    return {
      idx: String(i + 1).padStart(2, "0"),
      title: scene.title || `场景 ${i + 1}`,
      sceneId: sceneId || (hit && hit.sceneId) || null,
      paras: live ? hit.paras : [],
      live,
      missing: !live || (!!sceneId && missingIds.has(sceneId)),
    };
  });
  return {
    drama: manuDramaOf(chapter),
    scenes,
    live: scenes.some((scene) => scene.live),
    completion: ms.completion,
    missingSceneIds: [...missingIds],
    complete: manuCanonicalComplete(snapshot),
  };
}

/* ---------- 左栏与进度条 ---------- */

/* 目录章 → 成稿中心的章行。eligible 决定哪些章进成稿中心（ws-manuscripts-store 的
   manuscriptChapterEligible）；stage 是稿子走到哪一步（labels/catalog.js 的 chapterStage，与主页、章节编排同一条规则）。
   目录载荷里没有批准时间，所以章行也不带（以前的「于 … 批准」只有测试夹具填得出来）。 */
export function manuChapterRows(chapters, eligible) {
  return (chapters || []).filter(eligible || Boolean).map((c) => {
    const scenes = c.scenes || [];
    return {
      id: c.id,
      backendId: c.backendId || "",
      n: c.n,
      title: c.title,
      stage: chapterStage(c),
      current: !!c.current,
      words: (c.words && c.words.cur) || 0,
      scenes: scenes.length,
      sceneDone: scenes.filter((s) => s.state === "done").length,
    };
  });
}

/* 左栏分组：先放要你拍板的，定稿放最后。 */
export const MANU_LIST_GROUPS = [
  { key: "review", stages: ["review"] },
  { key: "draft", stages: ["draft"] },
  { key: "writing", stages: ["writing"] },
  { key: "todo", stages: ["todo", "planned"], label: "还没动笔" },
  { key: "approved", stages: ["approved"] },
];

export function manuListGroups(rows) {
  return MANU_LIST_GROUPS
    .map((g) => ({ ...g, label: g.label || chapterStateMeta(g.stages[0]).label, items: (rows || []).filter((c) => g.stages.includes(c.stage)) }))
    .filter((g) => g.items.length > 0);
}

/* 默认选中：项目的当前章（按章依次定稿，只有它批得了）→ 等你批准的 → 写作中的 → 第一章。 */
export function manuDefaultPick(rows) {
  const list = rows || [];
  return list.find((c) => c.current && c.stage !== "approved") || list.find((c) => c.stage === "review")
    || list.find((c) => c.stage === "writing") || list[0] || null;
}

/* 整书进度格：按目录全序（含还没进成稿中心的规划章），计划章数比目录多时补占位格。 */
export function manuProgressCells(chapters, planChapters) {
  const cells = (chapters || []).map((c) => ({ id: c.id, n: c.n, title: c.title, stage: chapterStage(c) }));
  for (let i = cells.length; i < (Number(planChapters) || 0); i++) {
    cells.push({ id: `plan${i}`, n: String(i + 1), title: "", stage: "planned", plan: true });
  }
  return cells;
}

/* 先去写哪一场：服务端说缺的第一场，没有就目录里第一场没写完的。 */
export function manuFirstMissingScene(chapter, snapshot) {
  const scenes = (chapter && chapter.scenes) || [];
  const missingIds = (snapshot && snapshot.body && snapshot.body.missingSceneIds) || [];
  return scenes.find((scene) => scene.backendId && missingIds.includes(scene.backendId))
    || scenes.find((scene) => scene.state !== "done")
    || null;
}

/* ---------- 导出 ---------- */

/* 导出文件里缺终稿的那一场留的一行：缺口写在明处，没写齐的章不会被当成写齐的一章 */
export const MANU_MISSING_SCENE_LINE = "（本场还没有终稿）";

/* 一个范围能不能导出；能就返回空串。snapshotOf(chapter) 给出该章的服务端快照。
   没同步、读失败、还在读的章拦下（编译只认刚从服务端读到的终稿）；读到了就能导出，写没写齐、正史核没核都不管——
   只有整个范围一个字的终稿都没有才拒绝。缺几场由 manuScopeNote 另说一句（2026-10：以前要每章写齐、正史核完才肯导出）。 */
export function manuScopeProblem(chapters, scopeIds, snapshotOf) {
  const selected = (chapters || []).filter((c) => scopeIds.includes(c.id));
  if (!selected.length) return "该范围内没有章节。";
  const unsynced = selected.filter((c) => !c.backendId);
  if (unsynced.length) return `有 ${unsynced.length} 章尚未同步到服务端。`;
  const snapshots = selected.map((c) => (snapshotOf && snapshotOf(c)) || MANU_IDLE_SNAPSHOT);
  const failed = snapshots.find((snapshot) => snapshot.status === "error");
  if (failed) return (failed.error && failed.error.message) || "服务端正文加载失败。";
  const pending = snapshots.filter((snapshot) => snapshot.status === "idle" || snapshot.status === "loading");
  if (pending.length) return `正在核验 ${pending.length} 章服务端正文…`;
  const anyText = selected.some((c, i) => { const body = manuBuildBody(c, snapshots[i]); return !!(body && body.live); });
  if (!anyText) return "该范围内还没有终稿正文。";
  return "";
}

/* 能导出、但范围里有没写齐的章时的一句说明（不是错误）；都写齐了就是空串。 */
export function manuScopeNote(chapters, scopeIds, snapshotOf) {
  let short = 0;
  let missing = 0;
  (chapters || []).filter((c) => scopeIds.includes(c.id)).forEach((c) => {
    const body = manuBuildBody(c, snapshotOf ? snapshotOf(c) : null);
    const gaps = body ? body.scenes.filter((s) => s.missing).length : 0;
    if (gaps) { short += 1; missing += gaps; }
  });
  return short ? `其中 ${short} 章还不完整（缺 ${missing} 场），文件里每个缺的场留一行「${MANU_MISSING_SCENE_LINE}」。` : "";
}

/* 导出里的章标题用完整章名（不截断）；和 labels 里的同名 chapterHeading（章号 / 章名两段）不是一回事，所以另起名字 */
function fullChapterTitle(chapter) {
  return chapterLabel(chapter, { maxTitle: Infinity });
}

/* 附录里的章的说明：叫法与分章面板、章节编排同一套（这一章给读者什么 / 先别揭；「先别揭」的几行用顿号连起来） */
function dramaLine(drama) {
  const value = (key) => String(drama[key] || "").split("\n").map((line) => line.trim()).filter(Boolean).join("、");
  return `章的说明 — ${MANU_DRAMA_KEYS.map((key) => `${dramaFieldLabel(key)}：${value(key) || "—"}`).join("；")}`;
}

/* 把 scopeIds 里的章（按目录顺序）编译成一个文件：{ name, content, mime }。
   fmt: md | txt | doc（Word 能打开的 HTML）。opts: { toc, appendix, snapshotOf }。 */
export function manuCompile(book, chapters, scopeIds, fmt, opts = {}) {
  const { toc = false, appendix = false, snapshotOf } = opts;
  const selected = (chapters || []).filter((c) => scopeIds.includes(c.id));
  const bodies = selected.map((c) => ({ c, body: manuBuildBody(c, snapshotOf ? snapshotOf(c) : null) }));
  const title = String((book && book.title) || "");

  if (fmt === "md") {
    const chunks = [`# ${title}\n`];
    if (book && book.kind) chunks.push(`> ${book.kind}\n`);
    if (toc) {
      chunks.push("\n## 目录\n");
      selected.forEach((c) => chunks.push(`- ${fullChapterTitle(c)}`));
      chunks.push("");
    }
    bodies.forEach(({ c, body }) => {
      chunks.push(`\n## ${fullChapterTitle(c)}\n`);
      if (body) {
        body.scenes.forEach((s) => {
          chunks.push(`### ${s.idx} · ${s.title}\n`);
          if (s.missing) chunks.push(MANU_MISSING_SCENE_LINE + "\n");
          else s.paras.forEach((p) => chunks.push(p + "\n"));
        });
      } else {
        chunks.push("（本章尚无正文）\n");
      }
      if (appendix) {
        const d = manuDramaOf(c);
        if (d) chunks.push(`> ${dramaLine(d)}\n`);
      }
    });
    return { name: `${title}.md`, content: chunks.join("\n"), mime: "text/markdown;charset=utf-8" };
  }

  if (fmt === "txt") {
    const chunks = [title + "\n"];
    bodies.forEach(({ c, body }) => {
      chunks.push(`\n\n${fullChapterTitle(c)}\n`);
      // 段首缩进两个全角空格（中文排版的习惯；以前是四个半角空格）
      if (body) {
        body.scenes.forEach((s) => {
          chunks.push("");
          if (s.missing) chunks.push(MANU_MISSING_SCENE_LINE);
          else s.paras.forEach((p) => chunks.push("\u3000\u3000" + p));
        });
      } else {
        chunks.push("（本章尚无正文）");
      }
    });
    return { name: `${title}.txt`, content: chunks.join("\n"), mime: "text/plain;charset=utf-8" };
  }

  /* Word 兼容的 HTML .doc：所有文字都经 escapeManuscriptText（与写作台同一个转义） */
  const esc = escapeManuscriptText;
  let html = `<html xmlns:w="urn:schemas-microsoft-com:office:word"><head><meta charset="utf-8"><title>${esc(title)}</title></head><body style="font-family:serif">`;
  html += `<h1>${esc(title)}</h1>`;
  if (toc) html += "<h2>目录</h2><ul>" + selected.map((c) => `<li>${esc(fullChapterTitle(c))}</li>`).join("") + "</ul>";
  bodies.forEach(({ c, body }) => {
    html += `<h2>${esc(fullChapterTitle(c))}</h2>`;
    if (body) {
      body.scenes.forEach((s) => {
        html += `<h3>${esc(s.idx)} · ${esc(s.title)}</h3>`
          + (s.missing ? `<p>${esc(MANU_MISSING_SCENE_LINE)}</p>` : s.paras.map((p) => `<p>${esc(p)}</p>`).join(""));
      });
    } else {
      html += "<p>（本章尚无正文）</p>";
    }
    if (appendix) {
      const d = manuDramaOf(c);
      if (d) html += `<blockquote><p>${esc(dramaLine(d))}</p></blockquote>`;
    }
  });
  html += "</body></html>";
  return { name: `${title}.doc`, content: html, mime: "application/msword;charset=utf-8" };
}
