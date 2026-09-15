import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { rvPush } from "./ws-review.jsx";
import { WsManuStore, manuscriptChapterEligible, manuscriptDisplayState } from "./ws-manuscripts-store.jsx";
import { dayTimeLabel } from "./lib/ago.js";

/* global React, I */
const { useState: useSt9, useRef: useRef9, useEffect: useEf9 } = React;

/* ==========================================================
   成稿中心 — Manuscript Center
   一本书在这里一章章成形：左侧按状态分组的章节，右侧
   沉浸式成稿阅读器，顶部「整书进度」书脊条 + 统一导出。
   ========================================================== */


/* ---------- 真实正文（Wave 1 · 治理 §5.2 换源）：唯一来源是后端章节聚合
   （WsManuStore ← GET /chapter-manuscripts/{id}，服务端以 FinalScene 归档行
   为源）。localStorage 的 wr-doc:* 是写作器编辑缓存，不再作为成稿正文来源
   ——清缓存不丢稿的前提是稿在后端。 ---------- */
function manuSnapshotOf(catCh) {
  if (!catCh || !catCh.backendId || !WsManuStore) return { status: "idle", body: null, error: null };
  if (WsManuStore.snapshot) return WsManuStore.snapshot(catCh.backendId);
  const body = WsManuStore.body ? WsManuStore.body(catCh.backendId) : null;
  return body ? { status: "ready", body, error: null } : { status: "idle", body: null, error: null };
}

function manuCanonicalComplete(snapshot) {
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

function manuCanonicalBlockReason(snapshot) {
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

function manuArchivedParas(catCh, s) {
  if (!catCh || !catCh.backendId || !s || !s.backendId || !WsManuStore) return null;
  const ms = manuSnapshotOf(catCh).body;
  if (!ms) return null;
  const hit = (ms.scenes || []).find(x => x.sceneId === s.backendId);
  return hit && hit.live && hit.paras.length ? hit.paras : null;
}
/* 目录戏剧卡 → 阅读器概要卡四字段 */
function manuDramaOf(c) {
  const d = (c && c.drama) || {};
  const pick = (v) => (v && v !== "—" ? v : "");
  if (!pick(d.promise) && !pick(d.spine) && !pick(d.arc) && !pick(d.aftertaste)) return null;
  return { promise: pick(d.promise), thrust: pick(d.spine), turn: pick(d.arc), after: pick(d.aftertaste) };
}
/* 只认服务端权威稿：目录章节没有 ready 快照就没有正文。 */
function manuBuildBody(catCh, suppliedSnapshot) {
  if (!catCh) return null;
  if (catCh.backendId) {
    const snapshot = suppliedSnapshot || manuSnapshotOf(catCh);
    if (!snapshot || snapshot.status !== "ready" || !snapshot.body) return null;
    const ms = snapshot.body;
    const archived = ms.scenes || [];
    const catalogScenes = catCh.scenes || [];
    const missingIds = new Set(ms.missingSceneIds || []);
    const source = catalogScenes.length ? catalogScenes : archived;
    const scenes = source.map((scene, i) => {
      const catalogShape = catalogScenes.length > 0;
      const sceneId = catalogShape ? scene.backendId : scene.sceneId;
      const hit = catalogShape
        ? (sceneId ? archived.find(entry => entry.sceneId === sceneId) : archived[i])
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
      drama: manuDramaOf(catCh),
      scenes,
      live: scenes.some(scene => scene.live),
      completion: ms.completion,
      missingSceneIds: [...missingIds],
      complete: manuCanonicalComplete(snapshot),
    };
  }
  return null;
}
/* ---------- 导出：编译真实内容并下载 ---------- */
function manuDownload(name, content, mime) {
  try {
    const blob = new Blob([content], { type: mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { try { URL.revokeObjectURL(a.href); a.remove(); } catch (e) {} }, 0);
    return true;
  } catch (e) { return false; }
}
/* 导出前把 scope 内各章的后端归档聚合拉齐（compile 同步读 store 缓存） */
async function manuRefreshChapters(catChs, scopeIds) {
  if (!WsManuStore) return;
  const targets = (catChs || []).filter(c => scopeIds.includes(c.id) && c.backendId);
  await Promise.all(targets.map(c => WsManuStore.refresh(c.backendId)));
}

function manuScopeProblem(catChs, scopeIds) {
  const selected = (catChs || []).filter(c => scopeIds.includes(c.id));
  if (!selected.length) return "该范围内没有章节。";
  const unsynced = selected.filter(c => !c.backendId);
  if (unsynced.length) return `有 ${unsynced.length} 章尚未同步到服务端。`;
  const snapshots = selected.map(manuSnapshotOf);
  const failed = snapshots.find(snapshot => snapshot.status === "error");
  if (failed) return (failed.error && failed.error.message) || "服务端正文加载失败。";
  const pending = snapshots.filter(snapshot => snapshot.status === "idle" || snapshot.status === "loading");
  if (pending.length) return `正在核验 ${pending.length} 章服务端正文…`;
  const incomplete = snapshots.filter(snapshot => !manuCanonicalComplete(snapshot));
  if (incomplete.length) return `有 ${incomplete.length} 章仍缺场景正文，暂不能导出。`;
  return "";
}

function manuCompile(book, catChs, scopeIds, fmt, opts) {
  const sel = (catChs || []).filter(c => scopeIds.includes(c.id));
  const chunks = [];
  const bodies = sel.map(c => ({ c, body: manuBuildBody(c) }));
  if (fmt === "md") {
    chunks.push(`# ${book.title}\n`);
    if (book.kind) chunks.push(`> ${book.kind}\n`);
    if (opts.toc) {
      chunks.push("\n## 目录\n");
      sel.forEach(c => chunks.push(`- 第 ${c.n} 章 · ${c.title}`));
      chunks.push("");
    }
    bodies.forEach(({ c, body }) => {
      chunks.push(`\n## 第 ${c.n} 章 · ${c.title}\n`);
      if (body && body.scenes) body.scenes.forEach(s => {
        chunks.push(`### ${s.idx} · ${s.title}\n`);
        s.paras.forEach(p => chunks.push(p + "\n"));
      });
      else chunks.push("（本章尚无正文）\n");
      if (opts.appendix) {
        const d = manuDramaOf(c);
        if (d) chunks.push(`> 戏剧卡 — 承诺：${d.promise || "—"}；推进：${d.thrust || "—"}；转变：${d.turn || "—"}；余味：${d.after || "—"}\n`);
      }
    });
    return { name: `${book.title}.md`, content: chunks.join("\n"), mime: "text/markdown;charset=utf-8" };
  }
  if (fmt === "txt") {
    chunks.push(book.title + "\n");
    bodies.forEach(({ c, body }) => {
      chunks.push(`\n\n第 ${c.n} 章 · ${c.title}\n`);
      if (body && body.scenes) body.scenes.forEach(s => { chunks.push(""); s.paras.forEach(p => chunks.push("    " + p)); });
      else chunks.push("（本章尚无正文）");
    });
    return { name: `${book.title}.txt`, content: chunks.join("\n"), mime: "text/plain;charset=utf-8" };
  }
  /* Word 兼容的 HTML .doc */
  const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  let html = `<html xmlns:w="urn:schemas-microsoft-com:office:word"><head><meta charset="utf-8"><title>${esc(book.title)}</title></head><body style="font-family:serif">`;
  html += `<h1>${esc(book.title)}</h1>`;
  if (opts.toc) html += "<h2>目录</h2><ul>" + sel.map(c => `<li>第 ${esc(c.n)} 章 · ${esc(c.title)}</li>`).join("") + "</ul>";
  bodies.forEach(({ c, body }) => {
    html += `<h2>第 ${esc(c.n)} 章 · ${esc(c.title)}</h2>`;
    if (body && body.scenes) body.scenes.forEach(s => {
      html += `<h3>${esc(s.idx)} · ${esc(s.title)}</h3>` + s.paras.map(p => `<p>${esc(p)}</p>`).join("");
    });
    else html += "<p>（本章尚无正文）</p>";
    if (opts.appendix) {
      const d = manuDramaOf(c);
      if (d) html += `<blockquote><p>戏剧卡 — 承诺：${esc(d.promise || "—")}；推进：${esc(d.thrust || "—")}；转变：${esc(d.turn || "—")}；余味：${esc(d.after || "—")}</p></blockquote>`;
    }
  });
  html += "</body></html>";
  return { name: `${book.title}.doc`, content: html, mime: "application/msword;charset=utf-8" };
}

const M_TONE = {
  approved: { tone: "sage",    label: "已批准", short: "终稿" },
  review:   { tone: "gold",    label: "审阅中", short: "待批" },
  draft:    { tone: "slate",   label: "草稿",   short: "草稿" },
  writing:  { tone: "crimson", label: "写作中", short: "在写" },
  plan:     { tone: "slate",   label: "计划中", short: "计划" },
};

function WsManuscripts({ go }) {
  /* 章节列表派生自 WsCatalog（与主页 / 写作器 / 编排台同源） */
  /* 订阅目录：批准 / 退回等动作写穿 WsCatalog 后这里自动刷新 */
  const catChs = useCatalogChapters ? useCatalogChapters() : (WsCatalog ? WsCatalog.get() : null);
  const chs = catChs
    ? catChs.filter(manuscriptChapterEligible).map(c => {
        const scenes = (c.scenes || []).length;
        const liveAt = c.approvedAt ? new Date(c.approvedAt).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : null;
        return {
          id: c.id, n: c.n, title: c.title, state: manuscriptDisplayState(c.state),
          words: (c.words && c.words.cur) || 0, scenes,
          ver: "v1", at: liveAt, by: liveAt ? "你" : null,
          sceneDone: (c.scenes || []).filter(s => s.state === "done").length,
        };
      })
    : [];

  const work = WsWorks ? WsWorks.active() : null;
  const book = {
    title: work ? work.title : "—",
    kind: work ? work.genre : "",
    goalWords: work ? work.wordsTarget : 0,
    planChapters: Math.max(work ? (work.chaptersTotal || 0) : 0, (catChs || []).length),
  };

  const approvedWords = chs.filter(c => c.state === "approved").reduce((s, c) => s + c.words, 0);
  const approvedCount = chs.filter(c => c.state === "approved").length;
  const reviewCount   = chs.filter(c => c.state === "review").length;
  const flowCount     = chs.filter(c => c.state === "draft" || c.state === "writing").length;
  const pct = Math.round((approvedWords / Math.max(1, book.goalWords)) * 100);

  const [pickedId, setPicked] = useSt9(() => {
    const w = chs.find(c => c.state === "review") || chs.find(c => c.state === "writing");
    return (w || chs[0] || {}).id;
  });
  const [view, setView] = useSt9("read");
  const [returnOpen, setReturnOpen] = useSt9(false);
  const [approvalOpen, setApprovalOpen] = useSt9(false);
  const [reopenOpen, setReopenOpen] = useSt9(false);
  const [aggregateState, setAggregateState] = useSt9({ busy: false, error: "", note: "" });
  const [workflowState, setWorkflowState] = useSt9({ busy: false, error: "", note: "" });
  const [chapterExportState, setChapterExportState] = useSt9({ busy: false, error: "", note: "" });
  const chapterExportBusyRef = useRef9(false);
  const chapterExportTokenRef = useRef9(0);
  const picked = chs.find(c => c.id === pickedId) || chs[0];
  const catPicked = (catChs || []).find(c => c.id === (picked && picked.id)) || null;
  /* Wave 1 换源：选中章拉后端归档聚合；loaded 事件驱动重渲染（store 同步缓存） */
  const [, manuBump] = useSt9(0);
  useEf9(() => {
    if (catPicked && catPicked.backendId && WsManuStore) {
      WsManuStore.refresh(catPicked.backendId).then(() => manuBump(n => n + 1));
    }
  }, [catPicked && catPicked.backendId]);
  useEf9(() => {
    const onLoaded = () => manuBump(n => n + 1);
    window.addEventListener("ws:manuscripts-loaded", onLoaded);
    return () => window.removeEventListener("ws:manuscripts-loaded", onLoaded);
  }, []);
  const canonical = manuSnapshotOf(catPicked);
  const canonicalComplete = manuCanonicalComplete(canonical);
  const canonicalBlockReason = manuCanonicalBlockReason(canonical);
  /* 有服务端章节时，正文只能来自权威聚合；error 不得退回演示稿。 */
  const body = catPicked ? manuBuildBody(catPicked, canonical) : null;
  const projectId = WsWorks ? WsWorks.activeId() : null;
  const refreshWorkflowSources = async (chapterId) => {
    if (WsCatalog && WsCatalog.__refresh) await WsCatalog.__refresh(projectId);
    if (WsWorks && WsWorks.__refresh) await WsWorks.__refresh();
    if (chapterId && WsManuStore) await WsManuStore.refresh(chapterId);
    manuBump(n => n + 1);
  };
  /* 送审闸门：由控制塔下发的章，章级审计（跨场连续性）未过时不默认放行 */
  const auditPending = () => false;
  const [gateArm, setGateArm] = useSt9(false);
  useEf9(() => { setGateArm(false); }, [pickedId]);
  useEf9(() => { setAggregateState({ busy: false, error: "", note: "" }); }, [pickedId]);
  useEf9(() => {
    chapterExportTokenRef.current += 1;
    chapterExportBusyRef.current = false;
    setChapterExportState({ busy: false, error: "", note: "" });
  }, [pickedId]);
  useEf9(() => {
    setWorkflowState({ busy: false, error: "", note: "" });
    setApprovalOpen(false);
    setReopenOpen(false);
  }, [pickedId]);
  const aggregateChapter = async () => {
    if (!catPicked || !catPicked.backendId || !WsManuStore || aggregateState.busy) return;
    setAggregateState({ busy: true, error: "", note: "" });
    try {
      const result = await WsManuStore.aggregate(catPicked.backendId);
      manuBump(n => n + 1);
      const status = (result && result.status) || "completed";
      setAggregateState({ busy: false, error: "", note: status === "created" ? "章节汇总已生成" : "章节汇总已刷新" });
    } catch (e) {
      setAggregateState({ busy: false, error: (e && e.message) || "章节汇总失败", note: "" });
    }
  };
  const retryCanonical = async () => {
    if (!catPicked || !catPicked.backendId || !WsManuStore) return;
    await WsManuStore.refresh(catPicked.backendId);
    manuBump(n => n + 1);
  };
  const submitToReview = async () => {
    if (!picked) return;
    const currentCanonical = manuSnapshotOf(catPicked);
    if (!manuCanonicalComplete(currentCanonical)) {
      setWorkflowState({ busy: false, error: `${manuCanonicalBlockReason(currentCanonical)}送审已暂停。`, note: "" });
      return;
    }
    if (auditPending(picked) && !gateArm) { setGateArm(true); return; }
    if (!projectId || !catPicked || !catPicked.backendId || !WsManuStore) {
      setWorkflowState({ busy: false, error: "章节尚未同步到服务端，暂时不能送审。", note: "" });
      return;
    }
    setGateArm(false);
    setWorkflowState({ busy: true, error: "", note: "" });
    try {
      await WsManuStore.setReviewState(projectId, catPicked.backendId, "review");
      await refreshWorkflowSources(catPicked.backendId);
      setWorkflowState({ busy: false, error: "", note: "已送入审阅；状态已由服务端确认。" });
    } catch (e) {
      setWorkflowState({ busy: false, error: (e && e.message) || "送审失败。", note: "" });
    }
  };
  /* 退回小修 = 理由 + 定位 + 待办，三者缺一不可；可顺手直达写作台深改姿态 */
  const doReturn = async ({ reason, sid, sceneTitle, openDeep }) => {
    if (!projectId || !catPicked || !catPicked.backendId || !WsManuStore || workflowState.busy) return;
    setWorkflowState({ busy: true, error: "", note: "" });
    try {
      await WsManuStore.setReviewState(projectId, catPicked.backendId, "draft");
      await refreshWorkflowSources(catPicked.backendId);
    } catch (e) {
      setWorkflowState({ busy: false, error: (e && e.message) || "退回小修失败。", note: "" });
      return;
    }
    if (rvPush) rvPush({
      kind: "qc", priority: 1,
      title: `第 ${picked.n} 章退回小修：${picked.title}`,
      where: `第 ${picked.n} 章${sceneTitle ? " · " + sceneTitle : ""}`,
      source: "成稿中心",
      detail: reason,
      actions: [
        { label: "直达深改 · 定位本场", intent: "primary", op: "nav", to: "writer", scene: sid, posture: "deep" },
        { label: "查看本章", intent: "ghost", op: "nav", to: "manuscripts" },
        { label: "标记完成", intent: "quiet", op: "resolve" },
      ],
    });
    setReturnOpen(false);
    setWorkflowState({ busy: false, error: "", note: "已退回草稿，并生成修订待办。" });
    if (openDeep && go) {
      go("writer", [
        ...(sid ? [{ type: "ws:writer-scene", detail: sid }] : []),
        { type: "ws:writer-posture", detail: "deep" },
      ]);
    }
  };

  const approveFinal = async ({ readNote, revisionNotes }) => {
    if (!projectId || !catPicked || !catPicked.backendId || !WsManuStore || workflowState.busy) return;
    const currentCanonical = manuSnapshotOf(catPicked);
    if (!manuCanonicalComplete(currentCanonical)) {
      setWorkflowState({ busy: false, error: `${manuCanonicalBlockReason(currentCanonical)}通读确认与批准已暂停。`, note: "" });
      return;
    }
    setWorkflowState({ busy: true, error: "", note: "" });
    try {
      await WsManuStore.confirmRead(projectId, catPicked.backendId, readNote);
      await WsManuStore.approveFinal(projectId, catPicked.backendId, revisionNotes);
      await refreshWorkflowSources(catPicked.backendId);
      setApprovalOpen(false);
      setWorkflowState({ busy: false, error: "", note: "终稿已由服务端批准并锁定。" });
    } catch (e) {
      setWorkflowState({ busy: false, error: (e && e.message) || "终稿批准失败。", note: "" });
    }
  };

  const reopenFinal = async ({ reason }) => {
    if (!projectId || !catPicked || !catPicked.backendId || !WsManuStore || workflowState.busy) return;
    setWorkflowState({ busy: true, error: "", note: "" });
    try {
      await WsManuStore.reopenFinal(projectId, catPicked.backendId, reason);
      await refreshWorkflowSources(catPicked.backendId);
      setReopenOpen(false);
      setWorkflowState({ busy: false, error: "", note: "终稿已重新打开；受影响的后续批准已由服务端撤销。" });
    } catch (e) {
      setWorkflowState({ busy: false, error: (e && e.message) || "重新打开终稿失败。", note: "" });
    }
  };

  const exportChapter = async (c) => {
    if (!c || chapterExportBusyRef.current) return;
    if (!canonicalComplete) {
      setChapterExportState({ busy: false, error: `${canonicalBlockReason}导出已暂停。`, note: "" });
      return;
    }
    chapterExportBusyRef.current = true;
    const token = chapterExportTokenRef.current + 1;
    chapterExportTokenRef.current = token;
    setChapterExportState({ busy: true, error: "", note: "" });
    try {
      await manuRefreshChapters(catChs, [c.id]);
      const refreshedChapter = (catChs || []).find(chapter => chapter.id === c.id);
      const refreshed = manuSnapshotOf(refreshedChapter);
      if (!manuCanonicalComplete(refreshed)) throw new Error(manuCanonicalBlockReason(refreshed));
      const out = manuCompile({ title: `${book.title} · 第 ${c.n} 章 ${c.title}`, kind: book.kind }, catChs || [], [c.id], "md", { toc: false, appendix: false });
      if (!manuDownload(out.name, out.content, out.mime)) throw new Error("浏览器未能生成下载文件。");
      if (chapterExportTokenRef.current === token) setChapterExportState({ busy: false, error: "", note: "本章已导出。" });
    } catch (e) {
      if (chapterExportTokenRef.current === token) setChapterExportState({ busy: false, error: (e && e.message) || "本章导出失败。", note: "" });
    } finally {
      if (chapterExportTokenRef.current === token) chapterExportBusyRef.current = false;
    }
  };

  // 选中没有正文/对比能力的章节时，回退到「正文」标签
  useEf9(() => {
    if (!picked) return;
    if (picked.state === "writing") { setView("read"); }
    if (view === "diff" && picked.state !== "approved" && picked.state !== "review") setView("read");
  }, [pickedId]); // eslint-disable-line

  /* 空白作品：还没有任何成稿 */
  if (!picked) {
    return (
      <div className="ms-page" data-screen-label="manuscripts · empty">
        <div style={{ display: "grid", placeItems: "center", minHeight: "70vh", textAlign: "center" }}>
          <div style={{ maxWidth: 420, display: "grid", gap: 14, justifyItems: "center" }}>
            <div style={{ fontFamily: "var(--font-serif)", fontSize: 22, color: "var(--ink-1)" }}>成稿中心还是空的</div>
            <p style={{ color: "var(--ink-3)", fontSize: 14, lineHeight: 1.8, margin: 0 }}>写出第一章后，章节会在这里一章章成形、送审、定稿。</p>
            <button className="btn btn-accent" onClick={() => go && go("writer")}><I.Pen size={15} /> 去写作</button>
          </div>
        </div>
      </div>
    );
  }

  const groups = [
    { key: "approved", label: "已批准终稿", items: chs.filter(c => c.state === "approved") },
    { key: "flow",     label: "流转中",     items: chs.filter(c => c.state === "review" || c.state === "draft") },
    { key: "writing",  label: "写作中",     items: chs.filter(c => c.state === "writing") },
    { key: "plan",     label: "待聚合",     items: chs.filter(c => c.state === "plan") },
  ];

  return (
    <div className="ms-page" data-screen-label="manuscripts">
      <ManuHero
        book={book} chapters={chs}
        stats={{ approvedWords, approvedCount, reviewCount, flowCount, pct }}
        pickedId={pickedId} onPick={setPicked}
        exportCtx={{ catChs: catChs || [], book, chs, pickedId }}
      />

      <div className="ms-cols">
        <aside className="ms-list">
          {groups.map(g => g.items.length > 0 && (
            <div key={g.key} className="ms-list-group">
              <div className="ms-list-grouphd">
                <span>{g.label}</span>
                <span className="ms-list-groupn">{g.items.length}</span>
              </div>
              <ul className="ms-list-items">
                {g.items.map(c => (
                  <li key={c.id}>
                    <button className={`ms-list-row ${pickedId === c.id ? "is-active" : ""}`} data-testid="manuscript-chapter-item" data-chapter-id={c.backendId || ""} onClick={() => setPicked(c.id)}>
                      <span className="ms-list-num">{c.n}</span>
                      <span className="ms-list-body">
                        <span className="ms-list-title text-serif">{c.title}</span>
                        <span className="ms-list-meta">
                          {c.words ? `${c.words.toLocaleString()} 字` : "未起稿"} · {c.scenes} 场
                          {c.state === "writing" && ` · 第 ${c.sceneDone}/${c.scenes} 场`}
                        </span>
                      </span>
                      <ManuState s={c.state} />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </aside>

        <section className="ms-reader">
          <header className="ms-reader-head">
            <div className="ms-reader-id">
              <div className="ms-reader-eyebrow">第 {picked.n} 章 · {picked.ver}</div>
              <h1 className="ms-reader-title text-serif">{picked.title}</h1>
              <div className="ms-reader-sub">
                {picked.scenes} 场 · {picked.words ? `${picked.words.toLocaleString()} 字` : "—"}
                {picked.state === "approved" && <span> · <I.Lock size={11} style={{verticalAlign:"-1px"}} /> 已批准{picked.at ? `于 ${picked.at} · ${picked.by}` : ""}</span>}
                {picked.state === "writing" && <span> · 已完成 {picked.sceneDone}/{picked.scenes} 场</span>}
              </div>
            </div>
            <div className="ms-reader-tools">
              <ManuState s={picked.state} big />
              {catPicked && catPicked.backendId && (
                <button className="btn btn-quiet btn-sm" data-testid="chapter-aggregate" onClick={aggregateChapter} disabled={aggregateState.busy}
                  title="以服务端归档的 FinalScene 为唯一来源，生成或刷新本章汇总">
                  {aggregateState.busy ? <I.Refresh size={13} className="sf-spin" /> : <I.Layers size={13} />}
                  {aggregateState.busy ? "汇总中…" : "生成/刷新章节汇总"}
                </button>
              )}
              <div className="seg">
                <button className={`seg-btn ${view === "read" ? "is-active" : ""}`} onClick={() => setView("read")}>{picked.state === "writing" ? "进度" : "正文"}</button>
                {picked.state !== "writing" && <button className={`seg-btn ${view === "structure" ? "is-active" : ""}`} onClick={() => setView("structure")}>结构</button>}
                <button className={`seg-btn ${view === "canon" ? "is-active" : ""}`} data-testid="manuscript-canon-tab" onClick={() => setView("canon")}>正史</button>
                {(picked.state === "approved" || picked.state === "review") && catPicked && (catPicked.scenes || []).length > 0 &&
                  <button className={`seg-btn ${view === "diff" ? "is-active" : ""}`} onClick={() => setView("diff")}>对比</button>}
              </div>
            </div>
          </header>
          {(aggregateState.error || aggregateState.note || workflowState.error || workflowState.note || chapterExportState.error || chapterExportState.note) && (
            <div role={(aggregateState.error || workflowState.error || chapterExportState.error) ? "alert" : "status"} style={{ margin: "0 18px 10px", color: (aggregateState.error || workflowState.error || chapterExportState.error) ? "var(--crimson)" : "var(--sage)", fontSize: 12.5 }}>
              {workflowState.error || chapterExportState.error || aggregateState.error || workflowState.note || chapterExportState.note || aggregateState.note}
            </div>
          )}
          {catPicked && catPicked.backendId && (picked.state === "writing" || view !== "read") && canonical.status === "error" && (
            <div className="ms-inline-error" role="alert">
              <span>{(canonical.error && canonical.error.message) || "服务端正文加载失败。"}</span>
              <button className="btn btn-ghost btn-sm" type="button" onClick={retryCanonical}><I.Refresh size={13} /> 重试加载</button>
            </div>
          )}
          {catPicked && catPicked.backendId && (picked.state === "writing" || view !== "read") && (canonical.status === "idle" || canonical.status === "loading") && (
            <div className="ms-canonical-loading" role="status"><I.Refresh size={13} className="sf-spin" /> 正在从服务端核验本章正文…</div>
          )}

          {view === "canon"
            ? <ManuCanon
                projectId={projectId}
                chapterId={catPicked && catPicked.backendId}
                canonical={canonical}
                onChanged={() => manuBump(n => n + 1)}
              />
            : picked.state === "writing"
              ? <ManuWriting picked={picked} go={go} gate={auditPending(picked) ? (gateArm ? "armed" : "pending") : null} onSubmit={submitToReview} canSubmit={canonicalComplete && !workflowState.busy} blockReason={canonicalBlockReason} />
              : (
              <>
                {view === "read"      && <ManuRead picked={picked} body={body} loadState={canonical} onRetry={retryCanonical} />}
                {view === "structure" && <ManuStructure picked={picked} body={body} catCh={catPicked} go={go} />}
                {view === "diff"      && <ManuDiff picked={picked} catCh={catPicked} />}
              </>
              )}

          <footer className="ms-reader-foot">
            <ManuFootNote picked={picked} canonical={canonical} canonicalComplete={canonicalComplete} />
            <div className="flex gap-2">
              {picked.state === "review" && (
                <>
                  <button className="btn btn-ghost" disabled={workflowState.busy} onClick={() => setReturnOpen(true)}>退回小修</button>
                  <button className="btn btn-accent" data-testid="approve-final-open" disabled={workflowState.busy || !canonicalComplete} title={!canonicalComplete ? canonicalBlockReason : undefined} onClick={() => setApprovalOpen(true)}><I.Check size={14} /> 批准为终稿</button>
                </>
              )}
              {picked.state === "draft" && (
                <button className="btn btn-accent" disabled={workflowState.busy || !canonicalComplete} title={!canonicalComplete ? canonicalBlockReason : undefined} onClick={submitToReview}>送入审阅</button>
              )}
              {picked.state === "approved" && (
                <>
                  <button className="btn btn-quiet btn-sm" data-testid="chapter-export" disabled={chapterExportState.busy || !canonicalComplete} title={!canonicalComplete ? canonicalBlockReason : undefined} onClick={() => exportChapter(picked)}>
                    {chapterExportState.busy ? <I.Refresh size={13} className="sf-spin" /> : <I.Download size={13} />} {chapterExportState.busy ? "导出中…" : "导出本章"}
                  </button>
                  <button className="btn btn-ghost" data-testid="reopen-final-open" disabled={workflowState.busy || !catPicked || !catPicked.backendId} onClick={() => setReopenOpen(true)}><I.Refresh size={13} /> 重新打开</button>
                </>
              )}
              {picked.state === "writing" && (
                <button className="btn btn-accent" onClick={() => go("writer")}><I.Pen size={13} /> 回写作房间续写</button>
              )}
            </div>
          </footer>
        </section>
      </div>

      {returnOpen && <ManuReturnModal picked={picked} catCh={catPicked} busy={workflowState.busy} error={workflowState.error} onClose={() => !workflowState.busy && setReturnOpen(false)} onConfirm={doReturn} />}
      {approvalOpen && <ManuApprovalModal picked={picked} busy={workflowState.busy} error={workflowState.error} onClose={() => !workflowState.busy && setApprovalOpen(false)} onConfirm={approveFinal} />}
      {reopenOpen && <ManuReopenModal picked={picked} busy={workflowState.busy} error={workflowState.error} onClose={() => !workflowState.busy && setReopenOpen(false)} onConfirm={reopenFinal} />}
    </div>
  );
}

/* ---------- 正史审核台：抽取只是候选，作者裁决后才进入后续写作上下文 ---------- */
const CANON_EVENT_LABELS = {
  character_state: "人物状态",
  character_learns: "人物获知",
  location_change: "位置变化",
  relation_change: "关系变化",
  item_change: "物品变化",
  foreshadow_plant: "伏笔埋设",
  foreshadow_reinforce: "伏笔强化",
  foreshadow_resolve: "伏笔兑现",
};

const CANON_STATUS_LABELS = {
  synced: "已提交正史",
  pending_review: "等待作者裁决",
  pending_extraction: "等待提取或人工确认",
  degraded: "自动提取降级",
  missing_final: "缺少终稿",
};

function ManuCanon({ projectId, chapterId, canonical, onChanged }) {
  const [busyKey, setBusyKey] = useSt9("");
  const [feedback, setFeedback] = useSt9({ error: "", note: "" });
  const [entityChoice, setEntityChoice] = useSt9({});
  const [verifyNotes, setVerifyNotes] = useSt9({});
  const [manualSceneId, setManualSceneId] = useSt9("");
  const [manual, setManual] = useSt9({
    event_type: "character_state",
    raw_entity_ref: "",
    fact_key: "",
    fact_value: "",
    evidence_text: "",
  });

  useEf9(() => {
    setBusyKey("");
    setFeedback({ error: "", note: "" });
    setEntityChoice({});
    setVerifyNotes({});
    setManualSceneId("");
  }, [chapterId]);

  if (!canonical || canonical.status === "idle" || canonical.status === "loading") {
    return <div className="ms-canon-empty" role="status"><I.Refresh size={15} className="sf-spin" /> 正在读取正史提交状态…</div>;
  }
  if (canonical.status === "error") {
    return <div className="ms-canon-empty is-error"><I.AlertTriangle size={17} /> {(canonical.error && canonical.error.message) || "正史状态加载失败。"}</div>;
  }

  const canon = canonical.body && canonical.body.canonContinuity;
  if (!canon) {
    return <div className="ms-canon-empty is-error"><I.AlertTriangle size={17} /> 服务端没有返回正史核验结果，终稿流转已安全暂停。</div>;
  }

  const scenes = canon.scenes || [];
  const run = async (key, action, successNote) => {
    if (busyKey) return;
    setBusyKey(key);
    setFeedback({ error: "", note: "" });
    try {
      await action();
      setFeedback({ error: "", note: successNote });
      if (onChanged) onChanged();
    } catch (e) {
      setFeedback({ error: (e && e.message) || "正史操作失败。", note: "" });
    } finally {
      setBusyKey("");
    }
  };

  const decide = (scene, candidate, action) => {
    const selected = entityChoice[candidate.candidate_id] || null;
    return run(
      `candidate:${candidate.candidate_id}`,
      () => WsManuStore.decideCanonCandidate(projectId, chapterId, candidate.candidate_id, {
        action,
        selected_entity_id: selected,
        expected_final_scene_row_id: scene.final_scene_row_id,
      }),
      action === "accept" ? "事实已接受；完成整场确认后才会进入正史。" : "候选已驳回，不会进入后续写作上下文。",
    );
  };

  const verify = (scene) => {
    const note = String(verifyNotes[scene.scene_id] || "").trim();
    if (!note) {
      setFeedback({ error: "请写明你核对了什么，再确认本场正史。", note: "" });
      return;
    }
    return run(
      `verify:${scene.scene_id}`,
      () => WsManuStore.verifySceneCanon(projectId, chapterId, scene.scene_id, {
        note,
        expected_final_scene_row_id: scene.final_scene_row_id,
      }),
      `第 ${scene.scene_seq} 场正史已由作者确认。`,
    );
  };

  const extract = (scene) => run(
    `extract:${scene.scene_id}`,
    () => WsManuStore.extractSceneCanon(projectId, chapterId, scene.scene_id),
    `第 ${scene.scene_seq} 场已重新提取，请逐条裁决。`,
  );

  const addManual = () => {
    if (!manualSceneId) {
      setFeedback({ error: "请先选择要补录事实的场景。", note: "" });
      return;
    }
    const payload = Object.fromEntries(Object.entries(manual).map(([key, value]) => [key, String(value || "").trim()]));
    if (!payload.raw_entity_ref || !payload.fact_key || !payload.fact_value || !payload.evidence_text) {
      setFeedback({ error: "实体、事实键、事实值和正文证据都必须填写。", note: "" });
      return;
    }
    return run(
      `manual:${manualSceneId}`,
      () => WsManuStore.createCanonCandidate(projectId, chapterId, manualSceneId, payload),
      "人工事实候选已加入，请继续接受或驳回。",
    );
  };

  return (
    <div className="ms-canon" data-testid="manuscript-canon-panel">
      <section className={`ms-canon-overview ${canon.complete ? "is-complete" : "is-pending"}`}>
        <div className="ms-canon-seal">{canon.complete ? <I.ShieldCheck size={24} /> : <I.Database size={22} />}</div>
        <div>
          <div className="ms-canon-kicker">CANON LEDGER · 正史账本</div>
          <h2 className="text-serif">{canon.complete ? "本章事实已经提交" : "本章仍有事实需要你落槌"}</h2>
          <p>AI 只能从终稿提出候选；只有你逐条裁决并完成整场确认后，人物状态、知识、关系与时间线才会原子进入后续章节。</p>
        </div>
        <div className="ms-canon-counts">
          <span><b>{canon.synced_scene_count || 0}</b> / {canon.scene_count || 0} 场已提交</span>
          <span><b>{canon.pending_candidate_count || 0}</b> 条待裁决</span>
        </div>
      </section>

      {(feedback.error || feedback.note) && (
        <div className={`ms-canon-feedback ${feedback.error ? "is-error" : "is-ok"}`} role={feedback.error ? "alert" : "status"}>
          {feedback.error || feedback.note}
        </div>
      )}

      <div className="ms-canon-scenes">
        {scenes.map(scene => {
          const pending = (scene.candidates || []).filter(candidate => candidate.status === "pending");
          const extraction = scene.extraction || {};
          return (
            <section className={`ms-canon-scene status-${scene.status}`} key={scene.scene_id} data-testid="canon-scene-card">
              <header>
                <div>
                  <span className="ms-canon-scene-no">SCENE {String(scene.scene_seq || "—").padStart(2, "0")}</span>
                  <strong>{CANON_STATUS_LABELS[scene.status] || scene.status}</strong>
                </div>
                <span className={`ms-canon-status ${scene.complete ? "is-complete" : "is-pending"}`}>
                  {scene.complete ? <I.CheckCircle size={13} /> : <I.Clock size={13} />}
                  {scene.complete ? "已提交" : `${pending.length} 条待裁决`}
                </span>
              </header>

              {!scene.complete && (extraction.extraction_outcome || ["pending_extraction", "degraded"].includes(scene.status)) && (
                <div className="ms-canon-extract-note">
                  <span>
                    提取结果：{extraction.extraction_outcome || "not_invoked"}
                    {extraction.extraction_reason ? ` · ${extraction.extraction_reason}` : ""}
                    {extraction.requires_empty_confirmation
                      ? " · 模型未发现持久变化，仍需你通读确认"
                      : extraction.requires_scene_confirmation
                        ? " · 候选裁决完成后仍需通读确认没有漏项"
                        : ""}
                  </span>
                  {["pending_extraction", "degraded"].includes(scene.status) && (
                    <button className="btn btn-quiet btn-sm" data-testid="canon-scene-extract" disabled={Boolean(busyKey)} onClick={() => extract(scene)}>
                      {busyKey === `extract:${scene.scene_id}` ? <I.Refresh size={12} className="sf-spin" /> : <I.Sparkles size={12} />}
                      用模型重新提取
                    </button>
                  )}
                </div>
              )}

              <div className="ms-canon-candidates">
                {(scene.candidates || []).map(candidate => {
                  const needsEntity = ["ambiguous", "unresolved"].includes(candidate.entity_resolution_status);
                  const evidenceGrounded = !candidate.evidence || candidate.evidence.grounded !== false;
                  const selected = entityChoice[candidate.candidate_id] || "";
                  const canAccept = evidenceGrounded && (!needsEntity || Boolean(selected));
                  return (
                    <article className={`ms-canon-candidate is-${candidate.status}`} key={candidate.candidate_id} data-testid="canon-candidate">
                      <div className="ms-canon-candidate-top">
                        <span>{CANON_EVENT_LABELS[candidate.event_type] || candidate.event_type}</span>
                        <span>{candidate.status === "pending" ? "候选" : candidate.status === "accepted" ? "已接受 · 待整场确认" : "已驳回"}</span>
                      </div>
                      <div className="ms-canon-fact">
                        <b>{candidate.raw_entity_ref}</b>
                        <span>{candidate.fact_key}</span>
                        <I.ArrowRight size={13} />
                        <strong>{candidate.fact_value}</strong>
                      </div>
                      <blockquote>“{(candidate.evidence && candidate.evidence.text) || "无证据摘录"}”</blockquote>
                      {!evidenceGrounded && <div className="ms-canon-evidence-warning"><I.AlertTriangle size={12} /> 这段引文不在当前终稿中，只能驳回后重新补录。</div>}
                      {candidate.status === "pending" && needsEntity && (
                        <label className="ms-canon-entity">
                          <span>{candidate.entity_resolution_status === "ambiguous" ? "同名/别名冲突，请指定实体" : "未识别实体；可驳回后用规范名称补录"}</span>
                          {(candidate.entity_options || []).length > 0 && (
                            <select value={selected} onChange={event => setEntityChoice(current => ({ ...current, [candidate.candidate_id]: event.target.value }))}>
                              <option value="">选择正史实体…</option>
                              {(candidate.entity_options || []).map(option => <option value={option.entity_id} key={option.entity_id}>{option.label}</option>)}
                            </select>
                          )}
                        </label>
                      )}
                      {candidate.status === "pending" && (
                        <div className="ms-canon-actions">
                          <button className="btn btn-ghost btn-sm" disabled={Boolean(busyKey)} onClick={() => decide(scene, candidate, "reject")}>驳回</button>
                          <button className="btn btn-accent btn-sm" data-testid="canon-candidate-accept" disabled={Boolean(busyKey) || !canAccept} onClick={() => decide(scene, candidate, "accept")}><I.Check size={13} /> 接受此事实</button>
                        </div>
                      )}
                    </article>
                  );
                })}
              </div>

              {!scene.complete && pending.length === 0 && scene.final_scene_row_id && (
                <div className="ms-canon-verify">
                  <label htmlFor={`canon-note-${scene.scene_id}`}>通读确认</label>
                  <textarea id={`canon-note-${scene.scene_id}`} value={verifyNotes[scene.scene_id] || ""} onChange={event => setVerifyNotes(current => ({ ...current, [scene.scene_id]: event.target.value }))} placeholder="例如：已通读本场，没有新的持久状态变化；人物伤势与上一场一致。" />
                  <button className="btn btn-accent btn-sm" data-testid="canon-scene-verify" disabled={Boolean(busyKey)} onClick={() => verify(scene)}><I.ShieldCheck size={13} /> 确认本场正史</button>
                </div>
              )}
            </section>
          );
        })}
      </div>

      {scenes.some(scene => scene.final_scene_row_id && !scene.complete) && <details className="ms-canon-manual">
        <summary><I.Plus size={13} /> 模型漏掉了事实？从终稿证据手工补录</summary>
        <div className="ms-canon-manual-grid">
          <label><span>场景</span><select value={manualSceneId} onChange={event => setManualSceneId(event.target.value)}><option value="">选择场景…</option>{scenes.filter(scene => scene.final_scene_row_id && !scene.complete).map(scene => <option key={scene.scene_id} value={scene.scene_id}>第 {scene.scene_seq} 场</option>)}</select></label>
          <label><span>类型</span><select value={manual.event_type} onChange={event => setManual(current => ({ ...current, event_type: event.target.value }))}>{Object.entries(CANON_EVENT_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label><span>实体规范名</span><input value={manual.raw_entity_ref} onChange={event => setManual(current => ({ ...current, raw_entity_ref: event.target.value }))} placeholder="如：林远" /></label>
          <label><span>事实键</span><input value={manual.fact_key} onChange={event => setManual(current => ({ ...current, fact_key: event.target.value }))} placeholder="如：injury" /></label>
          <label className="is-wide"><span>事实值</span><input value={manual.fact_value} onChange={event => setManual(current => ({ ...current, fact_value: event.target.value }))} placeholder="如：右臂骨折，需要固定六周" /></label>
          <label className="is-wide"><span>终稿原文证据（必须逐字存在）</span><textarea value={manual.evidence_text} onChange={event => setManual(current => ({ ...current, evidence_text: event.target.value }))} placeholder="粘贴当前终稿中的原句" /></label>
        </div>
        <button className="btn btn-ghost btn-sm" disabled={Boolean(busyKey)} onClick={addManual}>加入待审核候选</button>
      </details>}
    </div>
  );
}

/* ---------- Hero · 整书进度 ---------- */
function ManuHero({ book, chapters, stats, pickedId, onPick, exportCtx }) {
  const cells = [];
  for (let i = 0; i < book.planChapters; i++) {
    const ch = chapters[i];
    cells.push(ch ? { ...ch } : { id: `plan${i}`, n: String(i + 1).padStart(2, "0"), state: "plan", plan: true });
  }
  return (
    <header className="ms-hero">
      <div className="ms-hero-top">
        <div className="ms-hero-id">
          <div className="page-eyebrow" style={{margin:0}}>成稿中心 · MANUSCRIPT</div>
          <h1 className="ms-hero-title text-serif">{book.title}</h1>
          <span className="ms-hero-kind">{book.kind}</span>
        </div>

        <div className="ms-hero-stats">
          <Stat v={stats.approvedWords.toLocaleString()} k="已定稿字数" sub={`占目标 ${stats.pct}%`} />
          <Stat v={`${stats.approvedCount}/${book.planChapters}`} k="已批准章" sub="终稿锁定" />
          <Stat v={stats.reviewCount} k="待你批准" sub="审阅中" tone={stats.reviewCount ? "gold" : ""} />
          <Stat v={(book.goalWords - stats.approvedWords).toLocaleString()} k="距整书目标" sub={`${book.goalWords.toLocaleString()} 字`} />
        </div>

        <ManuExport ctx={exportCtx} />
      </div>

      <div className="ms-spine" role="img" aria-label={`整书 ${book.planChapters} 章进度`}>
        {cells.map(c => (
          <button
            key={c.id}
            className={`ms-spine-cell tone-${M_TONE[c.state].tone} ${c.state} ${pickedId === c.id ? "is-picked" : ""} ${c.plan ? "is-plan" : ""}`}
            title={`第 ${c.n} 章${c.title ? " · " + c.title : ""} — ${M_TONE[c.state].label}`}
            onClick={() => !c.plan && onPick(c.id)}
            disabled={c.plan}
          >
            <span className="ms-spine-n">{c.n}</span>
          </button>
        ))}
      </div>
      <div className="ms-spine-legend">
        <Leg tone="sage" label="已批准终稿" />
        <Leg tone="gold" label="审阅中" />
        <Leg tone="slate" label="草稿" />
        <Leg tone="crimson" label="写作中" />
        <Leg tone="plan" label="计划中" />
      </div>
    </header>
  );
}

function Stat({ v, k, sub, tone }) {
  return (
    <div className={`ms-stat ${tone ? "ms-stat-" + tone : ""}`}>
      <div className="ms-stat-v text-serif">{v}</div>
      <div className="ms-stat-k">{k}</div>
      <div className="ms-stat-sub">{sub}</div>
    </div>
  );
}
function Leg({ tone, label }) {
  return <span className="ms-leg"><span className={`ms-leg-dot tone-${tone}`} />{label}</span>;
}

/* ---------- 统一导出（逐章核验服务端权威正文后编译） ---------- */
function ManuExport({ ctx }) {
  const [open, setOpen] = useSt9(false);
  const [fmt, setFmt] = useSt9("md");
  const [scope, setScope] = useSt9("approved");
  const [toc, setToc] = useSt9(true);
  const [appendix, setAppendix] = useSt9(false);
  const [exportState, setExportState] = useSt9({ busy: false, error: "", note: "" });
  const ref = useRef9(null);
  const exportBusyRef = useRef9(false);
  const closeTimerRef = useRef9(null);

  useEf9(() => () => window.clearTimeout(closeTimerRef.current), []);

  useEf9(() => {
    if (!open) return;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  const { catChs = [], book = {}, chs = [], pickedId } = ctx || {};
  const approvedIds = chs.filter(c => c.state === "approved").map(c => c.id);
  const scopeIds =
    scope === "approved" ? approvedIds :
    scope === "current"  ? (pickedId ? [pickedId] : []) :
    chs.map(c => c.id);
  const scopeWords = chs.filter(c => scopeIds.includes(c.id)).reduce((s, c) => s + (c.words || 0), 0);
  const scopeKey = scopeIds.join("|");
  const scopeProblem = manuScopeProblem(catChs, scopeIds);
  const canExport = scopeIds.length > 0 && !scopeProblem && !exportState.busy;

  /* 弹层打开/范围切换时先水合所有目标章，生成按钮只在服务端逐章确认后放行。 */
  useEf9(() => {
    if (!open) return undefined;
    let live = true;
    setExportState({ busy: true, error: "", note: "" });
    manuRefreshChapters(catChs, scopeIds).then(() => {
      if (!live) return;
      const problem = manuScopeProblem(catChs, scopeIds);
      setExportState({ busy: false, error: problem, note: problem ? "" : "服务端正文已核验。" });
    }).catch((e) => {
      if (live) setExportState({ busy: false, error: (e && e.message) || "导出前核验失败。", note: "" });
    });
    return () => { live = false; };
  }, [open, scopeKey]); // eslint-disable-line

  const run = async () => {
    if (!canExport || exportBusyRef.current) return;
    exportBusyRef.current = true;
    setExportState({ busy: true, error: "", note: "" });
    try {
      await manuRefreshChapters(catChs, scopeIds);
      const problem = manuScopeProblem(catChs, scopeIds);
      if (problem) throw new Error(problem);
      const out = manuCompile(book, catChs, scopeIds, fmt, { toc, appendix });
      if (!manuDownload(out.name, out.content, out.mime)) throw new Error("浏览器未能生成下载文件。");
      setExportState({ busy: false, error: "", note: `已生成「${out.name}」` });
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = window.setTimeout(() => {
        setExportState({ busy: false, error: "", note: "" });
        setOpen(false);
      }, 1400);
    } catch (e) {
      setExportState({ busy: false, error: (e && e.message) || "导出失败。", note: "" });
    } finally {
      exportBusyRef.current = false;
    }
  };

  return (
    <div className="ms-export" ref={ref}>
      <button className={`btn btn-accent ms-export-btn ${open ? "is-open" : ""}`} onClick={() => setOpen(o => !o)}>
        <I.UploadCloud size={15} /> 统一导出
      </button>
      {open && (
        <div className="ms-export-pop">
          <div className="ms-export-hd">统一导出 · 发布</div>

          <div className="ms-export-field">
            <div className="ms-export-lab">范围</div>
            <div className="seg seg-block">
              <button className={`seg-btn ${scope === "all" ? "is-active" : ""}`} disabled={exportState.busy} onClick={() => setScope("all")}>全书</button>
              <button className={`seg-btn ${scope === "approved" ? "is-active" : ""}`} disabled={exportState.busy} onClick={() => setScope("approved")}>仅已批准 · {approvedIds.length}</button>
              <button className={`seg-btn ${scope === "current" ? "is-active" : ""}`} disabled={exportState.busy} onClick={() => setScope("current")}>当前章</button>
            </div>
          </div>

          <div className="ms-export-field">
            <div className="ms-export-lab">格式</div>
            <div className="seg seg-block">
              {[["md","Markdown"],["txt","纯文本"],["doc","Word"]].map(([k, l]) => (
                <button key={k} className={`seg-btn ${fmt === k ? "is-active" : ""}`} disabled={exportState.busy} onClick={() => setFmt(k)}>{l}</button>
              ))}
            </div>
          </div>

          <div className="ms-export-field">
            <div className="ms-export-lab">选项</div>
            <label className="ms-export-opt"><input type="checkbox" checked={toc} disabled={exportState.busy} onChange={e => setToc(e.target.checked)} /> 生成目录</label>
            <label className="ms-export-opt"><input type="checkbox" checked={appendix} disabled={exportState.busy} onChange={e => setAppendix(e.target.checked)} /> 附戏剧卡附录</label>
          </div>

          {exportState.error && <div className="ms-export-error" role="alert">{exportState.error}</div>}
          <div className="ms-export-foot">
            <span className="text-muted text-xs" role={exportState.busy || exportState.note ? "status" : undefined}>
              {exportState.busy ? "正在核验服务端正文…" : exportState.note || (!scopeProblem ? `约 ${scopeWords.toLocaleString()} 字 · ${scopeIds.length} 章` : scopeProblem)}
            </span>
            <button className="btn btn-accent btn-sm" data-testid="manuscript-export-run" disabled={!canExport} onClick={run}>
              {exportState.busy ? <I.Refresh size={13} className="sf-spin" /> : <I.Download size={13} />} {exportState.busy ? "核验中…" : "生成并下载"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- 状态药丸 ---------- */
function ManuState({ s, big }) {
  const m = M_TONE[s] || M_TONE.draft;
  return <span className={`pill pill-${m.tone} ${big ? "" : "text-xs"}`}><span className="pill-dot" />{m.label}</span>;
}

function ManuFootNote({ picked, canonical, canonicalComplete }) {
  if (!canonicalComplete) {
    const missing = (canonical && canonical.body && canonical.body.missingSceneIds) || [];
    const text = canonical && canonical.status === "error"
      ? "未能核验服务端正文；送审、通读确认、批准与导出已暂停。"
      : missing.length
        ? `服务端仍缺 ${missing.length} 场正文；送审、通读确认、批准与导出已暂停。`
        : "正在核验服务端正文；流转与导出暂不可用。";
    return <div className="text-muted text-sm">{text}</div>;
  }
  const note = {
    approved: <span><I.Lock size={12} style={{verticalAlign:"-2px"}} /> 已锁定终稿 · 不可编辑。需要修改请「重新打开」或回到章节编排。</span>,
    review:   <span>{picked.scenes} 场已成稿。检查无误后批准为终稿，章节将汇入整书。</span>,
    draft:    <span>草稿已写 {picked.words.toLocaleString()} 字。送审后才能进入批准流程。</span>,
    writing:  <span>第 {picked.sceneDone}/{picked.scenes} 场正在写。全部场景完成后才能进入审阅。</span>,
  };
  return <div className="text-muted text-sm">{note[picked.state]}</div>;
}

/* ---------- 正文阅读器 ---------- */
function ManuRead({ picked, body, loadState, onRetry }) {
  if (loadState && loadState.status === "error") {
    return (
      <div className="ms-empty ms-canonical-error" role="alert">
        <I.AlertTriangle size={26} />
        <div>服务端正文加载失败</div>
        <p>{(loadState.error && loadState.error.message) || "暂时无法核验本章权威稿。"}</p>
        <button className="btn btn-ghost btn-sm" type="button" data-testid="manuscript-retry" onClick={onRetry}><I.Refresh size={13} /> 重试加载</button>
      </div>
    );
  }
  if (!body || !body.scenes) {
    const loading = loadState && (loadState.status === "idle" || loadState.status === "loading");
    return <div className="ms-empty" role="status"><I.BookOpen size={26} /><div>{loading ? "正在从服务端核验本章正文…" : "本章正文尚未归档。"}</div></div>;
  }
  const missingCount = Math.max(
    (body.missingSceneIds || []).length,
    (body.scenes || []).filter(scene => scene.missing).length,
  );
  return (
    <article className="ms-read">
      <div className="ms-read-chno">第 {picked.n} 章</div>
      <h2 className="ms-read-htitle text-serif">{picked.title}</h2>
      {!body.complete && (
        <div className="ms-read-partial" role="status">
          <I.AlertTriangle size={14} /> 服务端仍缺 {missingCount || "若干"} 场正文，当前稿件不可送审或导出。
        </div>
      )}
      {body.scenes.map((s, i) => (
        <div key={s.sceneId || i} className={`ms-scene ${s.missing ? "is-missing" : ""}`}>
          <header className="ms-scene-head">
            <span className="ms-scene-idx">{s.idx}</span>
            <span className="ms-scene-title">{s.title}</span>
          </header>
          {s.missing
            ? <div className="ms-scene-missing"><I.Clock size={14} /> 这一场尚无服务端归档正文</div>
            : s.paras.map((p, j) => <p key={j} className="ms-scene-p">{p}</p>)}
        </div>
      ))}
      {body.complete && <div className="ms-read-end">— 章节结束 —</div>}
    </article>
  );
}

/* ---------- 写作中占位 ---------- */
function ManuWriting({ picked, go, onSubmit, gate, canSubmit, blockReason }) {
  const total = Math.max(1, picked.scenes || 0);
  const allDone = picked.scenes > 0 && picked.sceneDone >= picked.scenes;
  return (
    <div className="ms-writing">
      <div className="ms-writing-card">
        <div className="ms-writing-mark">{allDone ? <I.CheckCircle size={20} /> : <I.Pen size={20} />}</div>
        <h3 className="text-serif" style={{fontSize:19, margin:0}}>{allDone ? "全部场景已成稿" : "本章仍在写作"}</h3>
        <p className="text-muted text-sm" style={{maxWidth:380, lineHeight:1.7}}>
          {allDone
            ? <>「{picked.title}」的 {picked.scenes} 场全部完成，共 {picked.words.toLocaleString()} 字。送入审阅后即可批准为终稿。</>
            : <>「{picked.title}」已写 {picked.words.toLocaleString()} 字，完成 {picked.sceneDone}/{picked.scenes} 场。全部场景成稿后，会自动出现在成稿中心等待审阅。</>}
        </p>
        <div className="ms-writing-bar">
          <div className="ms-writing-fill" style={{width: `${((picked.sceneDone || 0) / total) * 100}%`}} />
        </div>
        {allDone && !canSubmit && <p className="text-muted text-xs" style={{margin:0, maxWidth:380}}>{blockReason}</p>}
        {allDone
          ? (gate ? (
            <div style={{ display: "grid", gap: 10, justifyItems: "center" }}>
              <p className="text-sm" style={{ margin: 0, maxWidth: 380, lineHeight: 1.7, color: gate === "armed" ? "var(--rose)" : "var(--ink-3)" }}>
                {gate === "armed"
                  ? "再次点击「仍要送审」将跳过章级审计直接送审——设定漂移 / 承诺回收将无人把关。"
                  : "本章由控制塔下发起草——送审前需先通过章级审计（跨场连续性把关）。"}
              </p>
              <div className="flex gap-2">
                <button className="btn btn-accent btn-sm" onClick={() => go("author")}><I.ShieldCheck size={13} /> 去章节编排 · 章级审计</button>
                <button className="btn btn-ghost btn-sm" disabled={!canSubmit} title={!canSubmit ? blockReason : undefined} onClick={onSubmit}>{gate === "armed" ? "仍要送审（带病）" : "跳过审计送审"}</button>
              </div>
            </div>
          ) : <button className="btn btn-accent btn-sm" disabled={!canSubmit} title={!canSubmit ? blockReason : undefined} onClick={onSubmit}><I.Check size={13} /> {canSubmit ? "送入审阅" : "等待归档核验"}</button>)
          : <button className="btn btn-accent btn-sm" onClick={() => go("writer")}><I.Pen size={13} /> 回写作房间续写</button>}
      </div>
    </div>
  );
}

/* ---------- 结构 ---------- */
/* 阶段 D：成稿后的场景三问（Ingermanson 的 Yes / No / Maybe 分诊）——准定稿评审随评审记录给出，
   目录按场透出（catalog story_check）。只是提示，不阻断任何流转。 */
const MS_STORY_VERDICT = { yes: ["Yes", "这一场成立"], no: ["No", "不成立"], maybe: ["Maybe", "能修"] };
function ManuStoryCheck({ check, sceneId, sid, go }) {
  if (!check) return <span className="ms-story-check is-none" />;
  const verdict = MS_STORY_VERDICT[check.verdict] || ["—", "未判"];
  const mark = (flag) => (flag === true ? "✓" : flag === false ? "✗" : "?");
  const title = `成稿后场景三问（准定稿评审）：坩埚可辨 ${mark(check.crucible_identified)} · 三拍落地 ${mark(check.shape_landed)} · 判定 ${verdict[0]}（${verdict[1]}）${check.note ? "\n" + check.note : ""}`;
  /* 阶段 R：原著的七步救治从这里回路——Maybe / No 都回第 10 步改形态与三拍再重写；No 还可以标记待删（回收站可恢复，不真删） */
  const backToPlan = () => {
    if (!go || !sceneId) return;
    // 视图意图在构思视图就绪后依次派发：先切到第 10 步，再选中这一场（ws-snow 自己处理挂载竞态）
    go("snowflake", [{ type: "ws:snow-step", detail: "planning" }, { type: "ws:snow-scene", detail: sceneId }]);
  };
  const markCut = () => {
    if (!sid || !(window.WsCatalog && window.WsCatalog.removeScenes)) return;
    if (!window.confirm("把这一场送进回收站（可恢复）？原著的做法是标记待删、下一稿再决定，不真删。")) return;
    window.WsCatalog.removeScenes([sid]);
  };
  return (
    <span className={`ms-story-check is-${check.verdict || "none"}`} title={title} data-testid="ms-story-check">
      <b>{verdict[0]}</b> 坩埚{mark(check.crucible_identified)} 三拍{mark(check.shape_landed)}
      {go && sceneId && check.verdict !== "yes" && (
        <button type="button" className="ms-story-check-act" data-testid="ms-story-check-plan" onClick={backToPlan} title="回第 10 步：先定这一场是主动还是反应场，写下三拍与坩埚，再重写、再评">回第 10 步</button>
      )}
      {sid && check.verdict === "no" && (
        <button type="button" className="ms-story-check-act" data-testid="ms-story-check-cut" onClick={markCut} title="标记待删：送进回收站，可恢复">标待删</button>
      )}
    </span>
  );
}
function ManuStructure({ picked, body, catCh, go }) {
  const drama = (body && body.drama) || manuDramaOf(catCh);
  /* 场景拼接：优先目录真实场景（含状态/字数），种子章回落演示归档 */
  const rows = catCh && (catCh.scenes || []).length
    ? catCh.scenes.map((s, i) => {
        const paras = manuArchivedParas(catCh, s);
        return {
          idx: String(i + 1).padStart(2, "0"), title: s.title,
          meta: paras ? `${paras.join("").length} 字 · 已归档` : (typeof s.words === "number" && s.words > 0 ? `${s.words.toLocaleString()} 字` : "未展开"),
          done: !!paras || s.state === "done",
          check: s.storyCheck || null,
          sceneId: s.backendId || "", sid: s.sid || "",
        };
      })
    : (body && body.scenes ? body.scenes.map(s => ({ idx: s.idx, title: s.title, meta: `${s.paras.join("").length} 字 · 已归档`, done: true })) : []);
  return (
    <div className="ms-struct">
      {drama && (
        <div className="card">
          <div className="card-head"><div className="card-title">戏剧卡 · 概要</div></div>
          <div className="ms-struct-grid">
            <div><div className="ms-struct-k">核心承诺</div><div className="ms-struct-v">{drama.promise || "—"}</div></div>
            <div><div className="ms-struct-k">主线推进</div><div className="ms-struct-v">{drama.thrust || "—"}</div></div>
            <div><div className="ms-struct-k">人物变化</div><div className="ms-struct-v">{drama.turn || "—"}</div></div>
            <div><div className="ms-struct-k">结尾余味</div><div className="ms-struct-v">{drama.after || "—"}</div></div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head"><div className="card-title">场景拼接 · {rows.length} 场</div></div>
        <ul className="ms-struct-scenes">
          {rows.map((s, i) => (
            <li key={i} className={s.done ? "" : "is-ghost"}>
              <span className="ms-scene-idx">{s.idx}</span>
              <span className={s.done ? "text-serif fw-600" : "text-muted"}>{s.title}</span>
              <ManuStoryCheck check={s.check} sceneId={s.sceneId} sid={s.sid} go={go} />
              <span className="text-muted text-sm">{s.meta}</span>
              {s.done ? <I.Check size={13} style={{color: "var(--sage)"}} /> : <I.Dot size={13} />}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/* ---------- 对比：正文修订历史（FE-ALIGN F2 接真，数据源 WrDocVersions） ---------- */
/* 委托 lib/ago.js dayTimeLabel（文案契约在那边：非法入参返回空串）。 */
const manuRevTime = dayTimeLabel;

function ManuDiff({ picked, catCh }) {
  const scenes = (catCh && catCh.scenes) || [];
  const [sid, setSid] = useSt9(() => (scenes[0] ? scenes[0].sid : null));
  const [vers, setVers] = useSt9(null);   // null = 列表加载中
  const [selNew, setSelNew] = useSt9(null);
  const [selOld, setSelOld] = useSt9(null);
  const [diff, setDiff] = useSt9(null);
  const [historyError, setHistoryError] = useSt9("");
  const [diffError, setDiffError] = useSt9("");
  const [historyRetry, setHistoryRetry] = useSt9(0);
  const [diffRetry, setDiffRetry] = useSt9(0);

  useEf9(() => {
    if (scenes.length && !scenes.some(s => s.sid === sid)) setSid(scenes[0].sid);
  }, [picked && picked.id, scenes.length]); // eslint-disable-line

  useEf9(() => {
    let on = true;
    setVers(null); setDiff(null); setSelNew(null); setSelOld(null); setHistoryError(""); setDiffError("");
    if (!sid || !window.WrDocVersions) { setVers([]); return undefined; }
    window.WrDocVersions.list(sid).then(items => {
      if (!on) return;
      setVers(items);
      if (items.length >= 2) { setSelNew(items[0].revisionNo); setSelOld(items[1].revisionNo); }
    }).catch((error) => {
      if (!on) return;
      setVers([]);
      setHistoryError((error && error.message) || "版本历史加载失败。");
    });
    return () => { on = false; };
  }, [sid, historyRetry]);

  useEf9(() => {
    let on = true;
    setDiff(null); setDiffError("");
    if (!sid || selNew == null || selOld == null || !window.WrDocVersions) return undefined;
    Promise.all([window.WrDocVersions.paras(sid, selOld), window.WrDocVersions.paras(sid, selNew)])
      .then(([a, b]) => { if (on) setDiff(window.WrDocVersions.diff(a, b)); })
      .catch((error) => {
        if (!on) return;
        setDiff(null);
        setDiffError((error && error.message) || "两个版本比对失败。");
      });
    return () => { on = false; };
  }, [sid, selNew, selOld, diffRetry]);

  const verLabel = (v) => `v${v.revisionNo} · ${manuRevTime(v.at) || "—"}${v.words ? ` · ${v.words} 字` : ""}`;
  const ready = vers && vers.length >= 2;
  return (
    <div className="ms-diff">
      <div className="ms-diff-head">
        <span className="text-muted text-sm">对比</span>
        {scenes.length > 1 && (
          <select className="select" style={{ maxWidth: 170 }} value={sid || ""} onChange={(e) => setSid(e.target.value)}>
            {scenes.map((s, i) => <option key={s.sid} value={s.sid}>{String(i + 1).padStart(2, "0")} · {s.title}</option>)}
          </select>
        )}
        {ready && (
          <>
            <select className="select" style={{ maxWidth: 190 }} value={selNew ?? ""}
              onChange={(e) => {
                const n = Number(e.target.value);
                setSelNew(n);
                if (selOld != null && selOld >= n) {
                  const older = vers.find(v => v.revisionNo < n);
                  setSelOld(older ? older.revisionNo : null);
                }
              }}>
              {vers.map(v => (
                <option key={v.revisionNo} value={v.revisionNo}>
                  {v.revisionNo === vers[0].revisionNo ? `${verLabel(v)} · 当前` : verLabel(v)}
                </option>
              ))}
            </select>
            <I.ArrowRight size={14} style={{color:"var(--ink-3)"}} />
            <select className="select" style={{ maxWidth: 190 }} value={selOld ?? ""} onChange={(e) => setSelOld(Number(e.target.value))}>
              {vers.filter(v => selNew == null || v.revisionNo < selNew).map(v => (
                <option key={v.revisionNo} value={v.revisionNo}>{verLabel(v)}</option>
              ))}
            </select>
            {diff && <span className="ms-diff-stat"><span className="d-add-dot" />+{diff.adds} 句</span>}
            {diff && <span className="ms-diff-stat"><span className="d-del-dot" />−{diff.dels} 句</span>}
          </>
        )}
      </div>
      <div className="ms-diff-body">
        {vers === null && <p className="ms-diff-p text-muted">正在加载版本历史…</p>}
        {historyError && (
          <div className="ms-inline-error" role="alert">
            <span>{historyError}</span>
            <button className="btn btn-ghost btn-sm" type="button" data-testid="manuscript-diff-history-retry" onClick={() => setHistoryRetry(n => n + 1)}><I.Refresh size={13} /> 重试</button>
          </div>
        )}
        {!historyError && vers && vers.length < 2 && (
          <p className="ms-diff-p text-muted">这一场还没有可对比的历史版本——在写作台再保存一次正文，这里就会出现两个版本。</p>
        )}
        {ready && !diff && !diffError && <p className="ms-diff-p text-muted">正在比对两个版本…</p>}
        {diffError && (
          <div className="ms-inline-error" role="alert">
            <span>{diffError}</span>
            <button className="btn btn-ghost btn-sm" type="button" data-testid="manuscript-diff-retry" onClick={() => setDiffRetry(n => n + 1)}><I.Refresh size={13} /> 重试</button>
          </div>
        )}
        {diff && diff.paras.map((pg, k) => (
          <p key={k} className="ms-diff-p">
            {pg.segs.map((sg, x) => (
              <span key={x} className={sg.t === "same" ? "d-same" : sg.t === "del" ? "d-del" : "d-add"}>{sg.text}</span>
            ))}
          </p>
        ))}
      </div>
    </div>
  );
}

/* ---------- 退回小修 · 理由 + 定位 + 待办 ---------- */
function ManuReturnModal({ picked, catCh, busy, error, onClose, onConfirm }) {
  const scenes = (catCh && catCh.scenes) || [];
  const [reason, setReason] = useSt9("");
  const [sid, setSid] = useSt9(() => (scenes[0] ? scenes[0].sid : null));
  const [openDeep, setOpenDeep] = useSt9(true);
  const ref = useRef9(null);
  useEf9(() => { if (ref.current) ref.current.focus(); }, []);
  useEf9(() => {
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  const sceneTitle = (scenes.find(s => s.sid === sid) || {}).title || "";
  const can = reason.trim().length > 0;
  return (
    <div className="mr-scrim" onClick={() => !busy && onClose()}>
      <div className="mr-card" role="dialog" aria-modal="true" aria-label="退回小修" onClick={(e) => e.stopPropagation()}>
        <header className="mr-head">
          <div>
            <div className="mr-title text-serif">退回小修 · 第 {picked.n} 章 {picked.title}</div>
            <div className="mr-sub">章状态回到「草稿」，并生成一条带定位的修订待办</div>
          </div>
          <button className="mr-x" onClick={onClose} disabled={busy} title="取消"><I.X size={16} /></button>
        </header>

        <label className="mr-field">
          <span className="mr-k">退回理由 <em>必填</em></span>
          <textarea ref={ref} className="mr-area" rows={3} value={reason}
            placeholder="写清楚为什么退、改哪里——这段话会原样出现在待办里。"
            onChange={(e) => setReason(e.target.value)} />
        </label>

        {scenes.length > 0 && (
          <div className="mr-field">
            <span className="mr-k">定位到场</span>
            <div className="mr-scenes">
              {scenes.map((s, i) => (
                <button key={s.sid} className={`mr-scene ${sid === s.sid ? "is-sel" : ""}`} onClick={() => setSid(s.sid)}>
                  <span className="mr-scene-n">{String(i + 1).padStart(2, "0")}</span>{s.title}
                </button>
              ))}
            </div>
          </div>
        )}

        <label className="mr-check">
          <input type="checkbox" checked={openDeep} disabled={busy} onChange={(e) => setOpenDeep(e.target.checked)} />
          退回后直接在写作台·深改姿态中打开这一场
        </label>

        {error && <div role="alert" className="mr-error">{error}</div>}

        <footer className="mr-foot">
          <button className="btn btn-ghost" disabled={busy} onClick={onClose}>取消</button>
          <button className="btn btn-accent" disabled={!can || busy} onClick={() => onConfirm({ reason: reason.trim(), sid, sceneTitle, openDeep })}>
            {busy ? "退回中…" : "退回并生成待办"}
          </button>
        </footer>
      </div>
    </div>
  );
}

/* ---------- 批准终稿 · 通读确认绑定正文哈希 ---------- */
function ManuApprovalModal({ picked, busy, error, onClose, onConfirm }) {
  const [read, setRead] = useSt9(false);
  const [readNote, setReadNote] = useSt9("");
  const [revisionNotes, setRevisionNotes] = useSt9("");
  const ref = useRef9(null);
  useEf9(() => { if (ref.current) ref.current.focus(); }, []);
  useEf9(() => {
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  return (
    <div className="mr-scrim" onClick={() => !busy && onClose()}>
      <div className="mr-card" role="dialog" aria-modal="true" aria-label="批准为终稿" onClick={(e) => e.stopPropagation()}>
        <header className="mr-head">
          <div>
            <div className="mr-title text-serif">批准终稿 · 第 {picked.n} 章 {picked.title}</div>
            <div className="mr-sub">确认会绑定当前服务端正文哈希；正文若变化，必须重新通读确认。</div>
          </div>
          <button className="mr-x" onClick={onClose} disabled={busy} title="取消"><I.X size={16} /></button>
        </header>

        <label className="mr-check mr-check-strong">
          <input ref={ref} data-testid="approve-read-confirm" type="checkbox" checked={read} disabled={busy} onChange={(e) => setRead(e.target.checked)} />
          我已从头到尾通读当前正文，并确认它可以成为终稿
        </label>

        <label className="mr-field">
          <span className="mr-k">通读备注 <small>可选</small></span>
          <textarea className="mr-area" rows={2} maxLength={1000} value={readNote} disabled={busy}
            placeholder="记录通读时重点核对了什么。"
            onChange={(e) => setReadNote(e.target.value)} />
        </label>

        <label className="mr-field">
          <span className="mr-k">终稿备注 <small>可选</small></span>
          <textarea className="mr-area" rows={2} maxLength={2000} value={revisionNotes} disabled={busy}
            placeholder="留给下一章或后续修订的提醒。"
            onChange={(e) => setRevisionNotes(e.target.value)} />
        </label>

        {error && <div role="alert" className="mr-error">{error}</div>}
        <footer className="mr-foot">
          <button className="btn btn-ghost" disabled={busy} onClick={onClose}>取消</button>
          <button className="btn btn-accent" data-testid="approve-final-confirm" disabled={!read || busy}
            onClick={() => onConfirm({ readNote: readNote.trim(), revisionNotes: revisionNotes.trim() })}>
            {busy ? "服务端批准中…" : "确认通读并批准"}
          </button>
        </footer>
      </div>
    </div>
  );
}

/* ---------- 重新打开终稿 · 有理由、可审计、级联失效 ---------- */
function ManuReopenModal({ picked, busy, error, onClose, onConfirm }) {
  const [reason, setReason] = useSt9("");
  const ref = useRef9(null);
  useEf9(() => { if (ref.current) ref.current.focus(); }, []);
  useEf9(() => {
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  const can = reason.trim().length > 0 && reason.length <= 1000;
  return (
    <div className="mr-scrim" onClick={() => !busy && onClose()}>
      <div className="mr-card" role="dialog" aria-modal="true" aria-label="重新打开终稿" onClick={(e) => e.stopPropagation()}>
        <header className="mr-head">
          <div>
            <div className="mr-title text-serif">重新打开 · 第 {picked.n} 章 {picked.title}</div>
            <div className="mr-sub">该章及其后已批准章节会失效，项目从本章重新推进；服务端会保留完整审计。</div>
          </div>
          <button className="mr-x" onClick={onClose} disabled={busy} title="取消"><I.X size={16} /></button>
        </header>
        <label className="mr-field">
          <span className="mr-k">重新打开原因 <em>必填</em></span>
          <textarea ref={ref} className="mr-area" rows={3} maxLength={1000} value={reason} disabled={busy}
            placeholder="说明为什么必须打破终稿锁；这段原因会进入审计记录。"
            onChange={(e) => setReason(e.target.value)} />
        </label>
        {error && <div role="alert" className="mr-error">{error}</div>}
        <footer className="mr-foot">
          <button className="btn btn-ghost" disabled={busy} onClick={onClose}>取消</button>
          <button className="btn btn-accent" data-testid="reopen-final-confirm" disabled={!can || busy} onClick={() => onConfirm({ reason: reason.trim() })}>
            {busy ? "重新打开中…" : "撤销批准并重新打开"}
          </button>
        </footer>
      </div>
    </div>
  );
}

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { WsManuscripts };
