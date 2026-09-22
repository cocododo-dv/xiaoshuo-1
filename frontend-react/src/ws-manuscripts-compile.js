import { escapeManuscriptText } from "./manuscript-html.js";
import { chapterLabel, chapterStateMeta, manuscriptStage } from "./ws-labels.js";

/* ==========================================================
   ws-manuscripts-compile — 成稿中心的纯函数
   ----------------------------------------------------------
   · 服务端权威稿的判定（能不能送审 / 批准 / 导出，卡在哪一步）；
   · 目录章 + 服务端聚合快照 → 阅读器用的逐场正文；
   · 目录 → 左栏的章行、分组与整书进度格；
   · 导出：把一个范围内的章编译成 Markdown / 纯文本 / Word 兼容 HTML。
   快照一律由调用方传入（通常是 WsManuStore.snapshot 的结果），这里不读 store、
   不碰 DOM、不写 window——所以能直接单测。
   ========================================================== */

const IDLE_SNAPSHOT = { status: "idle", body: null, error: null };

/* 服务端说这一章可以流转：聚合完整、没有缺场、正史核验完成。 */
export function manuCanonicalComplete(snapshot) {
  const body = snapshot && snapshot.body;
  return !!(
    snapshot && snapshot.status === "ready" && body
    && body.completion === "complete"
    && Array.isArray(body.missingSceneIds)
    && body.missingSceneIds.length === 0
    && body.canonContinuity
    && body.canonContinuity.complete === true
  );
}

/* 还不能流转时，一句话说清卡在哪。 */
export function manuCanonicalBlockReason(snapshot) {
  if (!snapshot || snapshot.status === "idle") return "正在等待服务端正文核验。";
  if (snapshot.status === "loading") return "正在从服务端加载权威正文。";
  if (snapshot.status === "error") return (snapshot.error && snapshot.error.message) || "服务端正文加载失败，请重试。";
  const missing = (snapshot.body && snapshot.body.missingSceneIds) || [];
  if (missing.length) return `服务端仍缺 ${missing.length} 场归档正文。`;
  const canon = snapshot.body && snapshot.body.canonContinuity;
  if (!canon) return "服务端尚未返回本章正史核验状态。";
  if ((canon.missing_final_scene_ids || []).length) return `仍有 ${canon.missing_final_scene_ids.length} 场缺少终稿，无法核验正史。`;
  if ((canon.pending_scene_ids || []).length) return `仍有 ${canon.pending_scene_ids.length} 场正史待核对。`;
  if (Number(canon.pending_candidate_count || 0) > 0) return `仍有 ${canon.pending_candidate_count} 条事实候选待裁决。`;
  return "服务端尚未将本章标记为可流转稿。";
}

/* 各场都已归档（缺的只是正史核对）——页脚据此把下一步指向「去核对正史」而不是写作台。 */
export function manuScenesArchived(snapshot) {
  const body = snapshot && snapshot.body;
  return !!(snapshot && snapshot.status === "ready" && body
    && (body.missingSceneIds || []).length === 0 && body.completion === "complete");
}

/* 目录戏剧卡 → 阅读器概要卡四字段；四项都空就没有戏剧卡。 */
export function manuDramaOf(chapter) {
  const d = (chapter && chapter.drama) || {};
  const pick = (v) => (v && v !== "—" ? v : "");
  if (!pick(d.promise) && !pick(d.spine) && !pick(d.arc) && !pick(d.aftertaste)) return null;
  return { promise: pick(d.promise), thrust: pick(d.spine), turn: pick(d.arc), after: pick(d.aftertaste) };
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
   manuscriptChapterEligible）；stage 是稿子走到哪一步（ws-labels.manuscriptStage）。
   目录载荷里没有批准时间，所以章行也不带（以前的「于 … 批准」只有测试夹具填得出来）。 */
export function manuChapterRows(chapters, eligible) {
  return (chapters || []).filter(eligible || Boolean).map((c) => {
    const scenes = c.scenes || [];
    return {
      id: c.id,
      backendId: c.backendId || "",
      n: c.n,
      title: c.title,
      stage: manuscriptStage(c),
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

/* 默认选中：等你批准的 → 写作中的 → 第一章。 */
export function manuDefaultPick(rows) {
  const list = rows || [];
  return list.find((c) => c.stage === "review") || list.find((c) => c.stage === "writing") || list[0] || null;
}

/* 整书进度格：按目录全序（含还没进成稿中心的规划章），计划章数比目录多时补占位格。 */
export function manuProgressCells(chapters, planChapters) {
  const cells = (chapters || []).map((c) => ({ id: c.id, n: c.n, title: c.title, stage: manuscriptStage(c) }));
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

/* 一个范围能不能导出；能就返回空串。snapshotOf(chapter) 给出该章的服务端快照。 */
export function manuScopeProblem(chapters, scopeIds, snapshotOf) {
  const selected = (chapters || []).filter((c) => scopeIds.includes(c.id));
  if (!selected.length) return "该范围内没有章节。";
  const unsynced = selected.filter((c) => !c.backendId);
  if (unsynced.length) return `有 ${unsynced.length} 章尚未同步到服务端。`;
  const snapshots = selected.map((c) => (snapshotOf && snapshotOf(c)) || IDLE_SNAPSHOT);
  const failed = snapshots.find((snapshot) => snapshot.status === "error");
  if (failed) return (failed.error && failed.error.message) || "服务端正文加载失败。";
  const pending = snapshots.filter((snapshot) => snapshot.status === "idle" || snapshot.status === "loading");
  if (pending.length) return `正在核验 ${pending.length} 章服务端正文…`;
  const incomplete = snapshots.filter((snapshot) => !manuCanonicalComplete(snapshot));
  if (incomplete.length) return `有 ${incomplete.length} 章仍缺场景正文，暂不能导出。`;
  return "";
}

function chapterHeading(chapter) {
  return chapterLabel(chapter, { maxTitle: Infinity });
}

function dramaLine(drama) {
  return `戏剧卡 — 承诺：${drama.promise || "—"}；推进：${drama.thrust || "—"}；转变：${drama.turn || "—"}；余味：${drama.after || "—"}`;
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
      selected.forEach((c) => chunks.push(`- ${chapterHeading(c)}`));
      chunks.push("");
    }
    bodies.forEach(({ c, body }) => {
      chunks.push(`\n## ${chapterHeading(c)}\n`);
      if (body) {
        body.scenes.forEach((s) => {
          chunks.push(`### ${s.idx} · ${s.title}\n`);
          s.paras.forEach((p) => chunks.push(p + "\n"));
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
      chunks.push(`\n\n${chapterHeading(c)}\n`);
      if (body) body.scenes.forEach((s) => { chunks.push(""); s.paras.forEach((p) => chunks.push("    " + p)); });
      else chunks.push("（本章尚无正文）");
    });
    return { name: `${title}.txt`, content: chunks.join("\n"), mime: "text/plain;charset=utf-8" };
  }

  /* Word 兼容的 HTML .doc：所有文字都经 escapeManuscriptText（与写作台同一个转义） */
  const esc = escapeManuscriptText;
  let html = `<html xmlns:w="urn:schemas-microsoft-com:office:word"><head><meta charset="utf-8"><title>${esc(title)}</title></head><body style="font-family:serif">`;
  html += `<h1>${esc(title)}</h1>`;
  if (toc) html += "<h2>目录</h2><ul>" + selected.map((c) => `<li>${esc(chapterHeading(c))}</li>`).join("") + "</ul>";
  bodies.forEach(({ c, body }) => {
    html += `<h2>${esc(chapterHeading(c))}</h2>`;
    if (body) {
      body.scenes.forEach((s) => {
        html += `<h3>${esc(s.idx)} · ${esc(s.title)}</h3>` + s.paras.map((p) => `<p>${esc(p)}</p>`).join("");
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
