import React from "react";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { rvPush } from "./ws-review.jsx";
import { WsManuStore } from "./ws-manuscripts-store.jsx";
import { wsToast } from "./ws-notify.jsx";
import { chapterLabel } from "./ws-labels.js";
import { manuCanonicalBlockReason, manuCanonicalComplete, manuCompile } from "./ws-manuscripts-compile.js";

/* ==========================================================
   ws-manuscripts-workflow — 成稿中心里跟服务端打交道的部分
   ----------------------------------------------------------
   · manuSnapshotOf / useManuCanonical：选中章的服务端聚合快照（WsManuStore 的同步缓存）；
   · manuRefreshChapters / manuDownload：导出前的逐章补拉与浏览器下载；
   · useManuWorkflow：送审、退回、批准、重新打开、刷新汇总、导出本章——每章一个状态对象
     （各动作各自的忙碌标记 + 一条状态消息），对话框开关也在这里。
   ========================================================== */

const { useCallback, useEffect, useRef, useState } = React;

const IDLE_SNAPSHOT = { status: "idle", body: null, error: null };

/* 目录章 → 服务端聚合快照。没有后端 id 的章就是 idle（没有正文可言）。 */
export function manuSnapshotOf(chapter) {
  if (!chapter || !chapter.backendId) return IDLE_SNAPSHOT;
  return WsManuStore.snapshot(chapter.backendId);
}

/* 选中章的权威快照：换章时拉一次，store 的 loaded 事件驱动重渲（store 是同步缓存）。
   bump 给动作用——动作改了服务端之后强制重读一遍快照。 */
export function useManuCanonical(chapter) {
  const [, setTick] = useState(0);
  const bump = useCallback(() => setTick((n) => n + 1), []);
  const backendId = chapter && chapter.backendId;
  useEffect(() => {
    if (backendId) WsManuStore.refresh(backendId).then(() => bump());
  }, [backendId, bump]);
  useEffect(() => {
    window.addEventListener("ws:manuscripts-loaded", bump);
    return () => window.removeEventListener("ws:manuscripts-loaded", bump);
  }, [bump]);
  return { snapshot: manuSnapshotOf(chapter), bump };
}

/* 导出前把范围内各章的服务端聚合拉齐（编译同步读 store 缓存）。
   打开导出面板、切换范围时只补拉「没有、失败或超过 30 秒」的章；真正生成时 force 全部重拉——
   以前每次打开 / 切换都把范围内每一章重拉一遍，全书范围就是 2×N 个请求。 */
const MANU_REUSE_MS = 30_000;
const manuFetchedAt = {};
export async function manuRefreshChapters(chapters, scopeIds, { force = true } = {}) {
  const now = Date.now();
  const targets = (chapters || []).filter((c) => scopeIds.includes(c.id) && c.backendId)
    .filter((c) => force || manuSnapshotOf(c).status !== "ready" || !(now - (manuFetchedAt[c.backendId] || 0) < MANU_REUSE_MS));
  await Promise.all(targets.map(async (c) => {
    await WsManuStore.refresh(c.backendId);
    manuFetchedAt[c.backendId] = Date.now();
  }));
}

export function manuDownload(name, content, mime) {
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

const IDLE_BUSY = { aggregate: false, workflow: false, export: false };
const IDLE_STATUS = { busy: IDLE_BUSY, message: null };

/* 章级流转。参数：picked（成稿中心的章行）、chapter（对应的目录章）、canonical（它的快照）、
   bump（重读快照）、book（{ title, kind }）、chapters（整份目录，导出本章用）、go（导航）。
   status.message 是最近一次动作的结果：{ tone: "danger" | "ok", text, scope }——
   以前三套 {busy, error, note} 按优先级拼成一条，旧的错误会压住新的成功提示。
   状态按章存：刷新汇总、送审、导出都不在模态框里，作者可以在请求途中点左栏换章。
   在途动作回来时写回它自己那一章（不报到新章头上）；回到那一章时，按钮仍按它的在途状态禁用。 */
export function useManuWorkflow({ picked, chapter, canonical, bump, book, chapters, go }) {
  const [byChapter, setByChapter] = useState({}); // 章 id → { busy, message }
  const [dialog, setDialog] = useState(null); // null | "return" | "approve" | "reopen"
  const exportBusyRef = useRef(new Set()); // 防双击：state 在同一帧里还来不及变，按章记
  const leftRef = useRef(null);
  const projectId = WsWorks.activeId();
  const pickedId = picked ? picked.id : null;
  const backendId = chapter && chapter.backendId;
  const status = (pickedId && byChapter[pickedId]) || IDLE_STATUS;

  const patch = (id, fn) => {
    if (!id) return;
    setByChapter((all) => ({ ...all, [id]: fn(all[id] || IDLE_STATUS) }));
  };

  /* 换章：离开的那一章的旧消息清掉（回来时不再看到一句早就过时的话；在途动作的结果仍会落到它头上），
     忙碌标记保留，对话框关上。 */
  useEffect(() => {
    const left = leftRef.current;
    leftRef.current = pickedId;
    if (left && left !== pickedId) patch(left, (s) => (s.message ? { ...s, message: null } : s));
    setDialog(null);
  }, [pickedId]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 动作开始时锁定章 id；收尾写回这一章，不管作者此刻在看哪一章。 */
  const begin = (scope) => {
    const id = pickedId;
    patch(id, (s) => ({ busy: { ...s.busy, [scope]: true }, message: null }));
    return {
      finish: (tone, text) => patch(id, (s) => ({ busy: { ...s.busy, [scope]: false }, message: text ? { tone, text, scope } : null })),
      fail: (text) => patch(id, (s) => ({ busy: { ...s.busy, [scope]: false }, message: { tone: "danger", text, scope } })),
    };
  };
  /* 动作还没开始就被拦下（前置条件不满足）：只报一句 */
  const refuse = (scope, text) => patch(pickedId, (s) => ({ ...s, message: { tone: "danger", text, scope } }));

  const refreshSources = async () => {
    await WsCatalog.__refresh(projectId);
    await WsWorks.__refresh();
    if (backendId) await WsManuStore.refresh(backendId);
    bump();
  };

  const aggregate = async () => {
    if (!backendId || status.busy.aggregate) return;
    const run = begin("aggregate");
    try {
      const result = await WsManuStore.aggregate(backendId);
      bump();
      run.finish("ok", (result && result.status) === "created" ? "章节汇总已生成" : "章节汇总已刷新");
    } catch (e) {
      run.fail((e && e.message) || "章节汇总失败");
    }
  };

  const retryCanonical = async () => {
    if (!backendId) return;
    await WsManuStore.refresh(backendId);
    bump();
  };

  const submitToReview = async () => {
    if (!picked || status.busy.workflow) return;
    const current = manuSnapshotOf(chapter);
    if (!manuCanonicalComplete(current)) {
      refuse("workflow", `${manuCanonicalBlockReason(current)}送审已暂停。`);
      return;
    }
    if (!projectId || !backendId) {
      refuse("workflow", "章节尚未同步到服务端，暂时不能送审。");
      return;
    }
    const run = begin("workflow");
    try {
      await WsManuStore.setReviewState(projectId, backendId, "review");
      await refreshSources();
      run.finish("ok", "已送入审阅；状态已由服务端确认。");
    } catch (e) {
      run.fail((e && e.message) || "送审失败。");
    }
  };

  /* 退回小修 = 理由 + 定位 + 待办，三者缺一不可；可顺手直达写作台深改姿态 */
  const returnToDraft = async ({ reason, sid, sceneTitle, openDeep }) => {
    if (!projectId || !backendId || status.busy.workflow) return;
    const run = begin("workflow");
    try {
      await WsManuStore.setReviewState(projectId, backendId, "draft");
      await refreshSources();
    } catch (e) {
      run.fail((e && e.message) || "退回小修失败。");
      return;
    }
    const head = chapterLabel(picked, { withTitle: false });
    rvPush({
      kind: "qc", priority: 1,
      // 「退回小修：第 3 章 · 盐场」；章名是占位的「第 3 章」时不写两遍
      title: `退回小修：${chapterLabel(picked, { maxTitle: Infinity })}`,
      where: `${head}${sceneTitle ? " · " + sceneTitle : ""}`,
      source: "成稿中心",
      detail: reason,
      actions: [
        { label: "直达深改 · 定位本场", intent: "primary", op: "nav", to: "writer", scene: sid, posture: "deep" },
        { label: "查看本章", intent: "ghost", op: "nav", to: "manuscripts" },
        { label: "标记完成", intent: "quiet", op: "resolve" },
      ],
    });
    setDialog(null);
    run.finish("ok", "已退回草稿，并生成修订待办。");
    if (openDeep && go) {
      go("writer", [
        ...(sid ? [{ type: "ws:writer-scene", detail: sid }] : []),
        { type: "ws:writer-posture", detail: "deep" },
      ]);
    }
  };

  const approveFinal = async ({ readNote, revisionNotes }) => {
    if (!projectId || !backendId || status.busy.workflow) return;
    const current = manuSnapshotOf(chapter);
    if (!manuCanonicalComplete(current)) {
      refuse("workflow", `${manuCanonicalBlockReason(current)}通读确认与批准已暂停。`);
      return;
    }
    const run = begin("workflow");
    try {
      await WsManuStore.confirmRead(projectId, backendId, readNote);
      await WsManuStore.approveFinal(projectId, backendId, revisionNotes);
      await refreshSources();
      setDialog(null);
      run.finish("ok", "终稿已由服务端批准并锁定。");
    } catch (e) {
      run.fail((e && e.message) || "终稿批准失败。");
    }
  };

  const reopenFinal = async ({ reason }) => {
    if (!projectId || !backendId || status.busy.workflow) return;
    const run = begin("workflow");
    try {
      await WsManuStore.reopenFinal(projectId, backendId, reason);
      await refreshSources();
      setDialog(null);
      run.finish("ok", "终稿已重新打开；受影响的后续批准已由服务端撤销。");
    } catch (e) {
      run.fail((e && e.message) || "重新打开终稿失败。");
    }
  };

  /* 导出本章：先按当前快照拦一次，再强制重拉服务端正文复核一次，都过了才下载。
     文件名与 Markdown 标题用完整章名（整书导出同样不截）。 */
  const exportChapter = async () => {
    if (!picked || exportBusyRef.current.has(picked.id)) return;
    if (!manuCanonicalComplete(canonical)) {
      refuse("export", `${manuCanonicalBlockReason(canonical)}导出已暂停。`);
      return;
    }
    const id = picked.id;
    exportBusyRef.current.add(id);
    const run = begin("export");
    try {
      await manuRefreshChapters(chapters, [id]);
      const refreshed = manuSnapshotOf((chapters || []).find((c) => c.id === id));
      if (!manuCanonicalComplete(refreshed)) throw new Error(manuCanonicalBlockReason(refreshed));
      const out = manuCompile(
        { title: `${book.title} · ${chapterLabel(picked, { maxTitle: Infinity })}`, kind: book.kind },
        chapters, [id], "md", { snapshotOf: manuSnapshotOf },
      );
      if (!manuDownload(out.name, out.content, out.mime)) throw new Error("浏览器未能生成下载文件。");
      run.finish("ok", "本章已导出。");
      // 文件确实下载了：作者换了章也要告诉他
      wsToast({ message: `已下载「${out.name}」`, tone: "ok" });
    } catch (e) {
      run.fail((e && e.message) || "本章导出失败。");
    } finally {
      exportBusyRef.current.delete(id);
    }
  };

  /* 打开对话框时清掉上一次流转留下的错误：重新打开同一个对话框，不该先看到一句旧的「批准失败」。 */
  const openDialog = (kind) => {
    patch(pickedId, (s) => (s.message && s.message.scope === "workflow" ? { ...s, message: null } : s));
    setDialog(kind);
  };
  /* 服务端还在处理时不许关：否则作者会以为操作取消了，实际服务端还在做。 */
  const closeDialog = () => { if (!status.busy.workflow) setDialog(null); };
  const workflowError = status.message && status.message.tone === "danger" && status.message.scope === "workflow"
    ? status.message.text
    : "";

  return {
    status, dialog, workflowError,
    openDialog, closeDialog,
    aggregate, retryCanonical, submitToReview, returnToDraft, approveFinal, reopenFinal, exportChapter,
  };
}
