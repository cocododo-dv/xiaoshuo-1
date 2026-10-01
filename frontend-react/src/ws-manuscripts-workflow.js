import React from "react";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { rvPush } from "./ws-review-store.js";
import { WsManuStore } from "./ws-manuscripts-store.jsx";
import { wsToast } from "./ws-notify.jsx";
import { writerIntents } from "./ws-finding-ui.jsx";
import { readyWorkId } from "./lib/ready-work.js";
import { chapterLabel } from "./labels/catalog.js";
import {
  MANU_IDLE_SNAPSHOT, manuCanonicalBlockReason, manuCanonicalComplete, manuCompile, manuReturnTodo,
} from "./ws-manuscripts-compile.js";

/* ==========================================================
   ws-manuscripts-workflow — 成稿中心里跟服务端打交道的部分
   ----------------------------------------------------------
   · manuSnapshotOf / useManuCanonical：选中章的服务端聚合快照（WsManuStore 的同步缓存）；
   · manuRefreshChapters / manuDownload：导出前的逐章补拉与浏览器下载；
   · useChapterActionStatus：每章一个动作状态（各动作各自的忙碌标记 + 一条状态消息）；
   · useManuWorkflow：送审、退回、批准、重新打开、导出本章，对话框开关也在这里。
   ========================================================== */

const { useCallback, useEffect, useRef, useState } = React;

/* 目录章 → 服务端聚合快照。没有后端 id 的章就是 idle（没有正文可言）。 */
export function manuSnapshotOf(chapter) {
  if (!chapter || !chapter.backendId) return MANU_IDLE_SNAPSHOT;
  return WsManuStore.snapshot(chapter.backendId);
}

/* 读过不到 30 秒、目录之后也没变过的快照直接用（WsManuStore.refresh 的 maxAgeMs）：左栏来回点章、打开导出面板、
   切换导出范围都不再逐章重读服务端聚合（审计 F04-08）。动作之后与真正生成导出时照旧强制重读。 */
const MANU_REUSE_MS = 30_000;

/* 选中章的权威快照：换章时读一次（新鲜的快照直接用），store 一变（WsManuStore.subscribe）就重渲（store 是同步缓存）。
   bump 给动作用——动作改了服务端之后强制重读一遍快照。 */
export function useManuCanonical(chapter) {
  const [, setTick] = useState(0);
  const bump = useCallback(() => setTick((n) => n + 1), []);
  const backendId = chapter && chapter.backendId;
  useEffect(() => {
    if (backendId) WsManuStore.refresh(backendId, { maxAgeMs: MANU_REUSE_MS }).then(() => bump());
  }, [backendId, bump]);
  useEffect(() => WsManuStore.subscribe(bump), [bump]);
  return { snapshot: manuSnapshotOf(chapter), bump };
}

/* 导出前把范围内各章的服务端聚合拉齐（编译同步读 store 缓存）。
   打开导出面板、切换范围时只补拉「没有、失败或不新鲜」的章（maxAgeMs）；真正生成时 force 全部重拉——
   以前每次打开 / 切换都把范围内每一章重拉一遍，全书范围就是 2×N 个请求。 */
export async function manuRefreshChapters(chapters, scopeIds, { force = true } = {}) {
  const targets = (chapters || []).filter((c) => scopeIds.includes(c.id) && c.backendId);
  await Promise.all(targets.map((c) => WsManuStore.refresh(c.backendId, force ? {} : { maxAgeMs: MANU_REUSE_MS })));
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

const IDLE_BUSY = { workflow: false, export: false };
const IDLE_STATUS = { busy: IDLE_BUSY, message: null };

/* 每章一个动作状态 { busy: { workflow, export }, message }。message 是最近一次动作的结果：
   { tone: "danger" | "ok", text, scope }——以前三套 {busy, error, note} 按优先级拼成一条，旧的错误会压住新的成功提示。
   状态按章存：送审、导出都不在模态框里，作者可以在请求途中点左栏换章。begin(scope) 在动作开始时锁定章 id，
   finish / fail 写回这一章，不管作者此刻在看哪一章；回到那一章时，按钮仍按它的在途状态禁用。
   换章时离开的那一章的旧消息清掉（在途动作的结果仍会落到它头上），忙碌标记保留。 */
export function useChapterActionStatus(pickedId) {
  const [byChapter, setByChapter] = useState({}); // 章 id → { busy, message }
  const leftRef = useRef(null);
  const status = (pickedId && byChapter[pickedId]) || IDLE_STATUS;

  const patch = (id, fn) => {
    if (!id) return;
    setByChapter((all) => ({ ...all, [id]: fn(all[id] || IDLE_STATUS) }));
  };

  useEffect(() => {
    const left = leftRef.current;
    leftRef.current = pickedId;
    if (left && left !== pickedId) patch(left, (s) => (s.message ? { ...s, message: null } : s));
  }, [pickedId]); // eslint-disable-line react-hooks/exhaustive-deps

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
  /* 清掉这一章某一类动作留下的消息（重新打开对话框时不先看到上一次的错误） */
  const clearMessage = (scope) => patch(pickedId, (s) => (s.message && s.message.scope === scope ? { ...s, message: null } : s));

  return { status, begin, refuse, clearMessage };
}

/* 章级流转。参数：picked（成稿中心的章行）、chapter（对应的目录章）、canonical（它的快照）、
   bump（重读快照）、book（{ title, kind }）、chapters（整份目录，导出本章用）、go（导航）。 */
export function useManuWorkflow({ picked, chapter, canonical, bump, book, chapters, go }) {
  const [dialog, setDialog] = useState(null); // null | "return" | "approve" | "reopen"
  const exportBusyRef = useRef(new Set()); // 防双击：state 在同一帧里还来不及变，按章记
  const projectId = readyWorkId(WsWorks);
  const pickedId = picked ? picked.id : null;
  const backendId = chapter && chapter.backendId;
  const { status, begin, refuse, clearMessage } = useChapterActionStatus(pickedId);

  /* 换章：对话框关上 */
  useEffect(() => { setDialog(null); }, [pickedId]);

  const refreshSources = async () => {
    await WsCatalog.refresh(projectId);
    await WsWorks.retry("projects");
    if (backendId) await WsManuStore.refresh(backendId);
    bump();
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
    rvPush(manuReturnTodo(picked, { reason, sid, sceneTitle }));
    setDialog(null);
    run.finish("ok", "已退回草稿，并生成修订待办。");
    /* 直达深改：定位到场时进那一场的深改姿态；没定位到场也照样进深改姿态 */
    if (openDeep && go) go("writer", sid ? writerIntents(sid, { deep: true }) : [{ type: "ws:writer-posture", detail: "deep" }]);
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
      // 「已通读」随「确认定稿」一次提交，绑定作者读到的那一份正文（批准 #10）
      await WsManuStore.approveFinal(projectId, backendId, { readNote, revisionNotes });
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
    clearMessage("workflow");
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
    retryCanonical, submitToReview, returnToDraft, approveFinal, reopenFinal, exportChapter,
  };
}
