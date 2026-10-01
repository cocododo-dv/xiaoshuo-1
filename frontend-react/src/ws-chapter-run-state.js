import React from "react";
import { apiGet, apiPost } from "./lib/client.js";
import { createPoller } from "./lib/poll.js";
import { readyWorkId } from "./lib/ready-work.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsDiagnosis } from "./ws-diagnosis-summary.jsx";
import { WsWorks } from "./ws-works.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { ACTIVE_STATUSES, EMPTY_RUN, TERMINAL_STATUSES, normalizeRun, runErrorMessage } from "./ws-chapter-run-model.js";

/* ==========================================================
   章节编排「运行本章」的状态与请求（2026-09-29 从 ws-chapter-run.jsx 拆出）
   ----------------------------------------------------------
   useChapterRun({ chapter, onCatalogRefresh, pollIntervalMs })：挂载 / 切章先从 run-status 水合；
   提交（先确认要跑几场）；进行中按 pollIntervalMs 轮询到终态——经 lib/poll.js，页面隐藏时放慢、
   回到前台立刻问一次（过去后台标签页里也每 1.2 秒问一次）；亲眼看着跑完才重拉目录；后台又归档了几场
   时刷新这一章的诊断角标。旧章 / 卸载后的回包用 token 丢弃。ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useRef, useState } = React;

export function useChapterRun({ chapter, onCatalogRefresh, pollIntervalMs = 1200 }) {
  const [run, setRun] = useState(EMPTY_RUN);
  const [hydration, setHydration] = useState({ status: "loading", chapterKey: null, errorCode: null, message: "" });
  const [cardOpen, setCardOpen] = useState(false);
  const mountedRef = useRef(true);
  const pollerRef = useRef(null);                 // 进行中的运行：看页面可见性的轮询（lib/poll.js）
  const requestRef = useRef(0);
  const submittingRef = useRef(false);
  const confirmingRef = useRef(false);
  const completedRef = useRef(null);
  const statusRef = useRef(EMPTY_RUN.status);   // 上一次看到的状态：判断这次是不是「亲眼看着」的变化
  const completedCountRef = useRef(0);          // 上一次看到的已完成场数：多了就刷新这一章的诊断角标

  const clearPoll = () => {
    if (pollerRef.current) {
      pollerRef.current.stop();
      pollerRef.current = null;
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestRef.current += 1;
      submittingRef.current = false;
      clearPoll();
    };
  }, []);

  const isCurrent = (token) => mountedRef.current && requestRef.current === token;

  const refreshCompletedCatalog = async (projectId, chapterId, nextRun, token) => {
    const completionKey = `${chapterId}:${nextRun.jobId || "completed"}`;
    if (completedRef.current === completionKey) return;
    completedRef.current = completionKey;
    try {
      await WsCatalog.refresh(projectId);
      if (!isCurrent(token)) return;
      if (onCatalogRefresh) onCatalogRefresh(WsCatalog.get());
    } catch (error) {
      if (!isCurrent(token)) return;
      setRun((current) => current.status === "completed"
        ? { ...current, refreshWarning: "运行已完成，但目录刷新失败；请稍后手动刷新。" }
        : current);
    }
  };

  const consume = (payload, { token, projectId, chapterId }) => {
    if (!isCurrent(token)) return;
    const nextRun = normalizeRun(payload);
    const wasLive = ACTIVE_STATUSES.has(statusRef.current);
    if (!nextRun) {
      clearPoll();
      statusRef.current = "failed";
      setCardOpen(true);
      setRun({
        ...EMPTY_RUN,
        status: "failed",
        errorCode: "CHAPTER_RUN_STATUS_INVALID",
        message: "后端没有返回可识别的章节运行状态，请稍后重试。",
      });
      return;
    }
    // 后台作业又归档了几场：这一章的诊断角标随之更新（服务端改了这几场的正文，浏览器这边没有别的信号）
    if (wasLive && nextRun.completedCount > completedCountRef.current) {
      try { WsDiagnosis.refreshChapter(chapterId); } catch (error) { /* 角标不是闸门 */ }
    }
    completedCountRef.current = nextRun.completedCount;
    statusRef.current = nextRun.status;
    setRun(nextRun);
    // 进行中的运行总是摊开；终态只在作者看着它从进行中走到终态时弹出
    if (ACTIVE_STATUSES.has(nextRun.status)) setCardOpen(true);
    else if (TERMINAL_STATUSES.has(nextRun.status)) setCardOpen(wasLive);
    if (ACTIVE_STATUSES.has(nextRun.status)) {
      /* 还在跑：接着问（页面隐藏时放慢，回到前台立刻问一次）；这一轮的轮询已经在跑就不重建 */
      if (!pollerRef.current || pollerRef.current.token !== token) {
        clearPoll();
        const poller = createPoller({
          interval: Math.max(10, pollIntervalMs),
          run: async () => {
            await poll({ token, projectId, chapterId });
            return isCurrent(token) && ACTIVE_STATUSES.has(statusRef.current);
          },
        });
        poller.token = token;
        pollerRef.current = poller;
        poller.start();
      }
      return;
    }
    clearPoll();
    // 只有这一次亲眼看着跑完才重拉目录；打开一章时读到「上次已完成」不再每次重拉整份目录
    if (nextRun.status === "completed" && wasLive) {
      void refreshCompletedCatalog(projectId, chapterId, nextRun, token);
    }
  };

  const poll = async ({ token, projectId, chapterId }) => {
    if (!isCurrent(token)) return;
    try {
      const payload = await apiGet(`/api/v1/chapters/${encodeURIComponent(chapterId)}/run-status`);
      consume(payload, { token, projectId, chapterId });
    } catch (error) {
      if (!isCurrent(token)) return;
      clearPoll();
      statusRef.current = "failed";
      setCardOpen(true);
      setRun({
        ...EMPTY_RUN,
        status: "failed",
        errorCode: (error && error.code) || "CHAPTER_RUN_STATUS_UNAVAILABLE",
        message: (error && error.message) || "暂时无法查询章节运行进度，请重试。",
      });
    }
  };

  const hydrateStatus = async ({ token, projectId, chapterId, chapterKey }) => {
    if (!isCurrent(token)) return;
    setHydration({ status: "loading", chapterKey, errorCode: null, message: "" });
    try {
      const payload = await apiGet(`/api/v1/chapters/${encodeURIComponent(chapterId)}/run-status`);
      if (!isCurrent(token)) return;
      if (!normalizeRun(payload)) {
        const error = new Error("后端没有返回可识别的章节运行状态。");
        error.code = "CHAPTER_RUN_STATUS_INVALID";
        throw error;
      }
      setHydration({ status: "ready", chapterKey, errorCode: null, message: "" });
      consume(payload, { token, projectId, chapterId });
    } catch (error) {
      if (!isCurrent(token)) return;
      clearPoll();
      statusRef.current = EMPTY_RUN.status;
      setRun(EMPTY_RUN);
      setHydration({
        status: "error",
        chapterKey,
        errorCode: (error && error.code) || "CHAPTER_RUN_STATUS_UNAVAILABLE",
        message: (error && error.message) || "暂时无法查询章节运行状态。",
      });
    }
  };

  /* mount/切章必须先从 run-status 水合，旧章请求用 token 丢弃。 */
  useEffect(() => {
    const chapterId = chapter && chapter.backendId;
    const chapterKey = chapterId || (chapter && chapter.id) || "";
    const projectId = readyWorkId(WsWorks);
    const token = requestRef.current + 1;
    requestRef.current = token;
    submittingRef.current = false;
    confirmingRef.current = false;
    completedRef.current = null;
    statusRef.current = EMPTY_RUN.status;
    clearPoll();
    setRun(EMPTY_RUN);
    setCardOpen(false);
    if (!chapterId) {
      setHydration({
        status: "error",
        chapterKey,
        errorCode: "CHAPTER_NOT_SYNCED",
        message: "当前章节尚未同步到后端，暂时无法核验运行状态。",
      });
      return;
    }
    if (!projectId) {
      setHydration({
        status: "error",
        chapterKey,
        errorCode: "PROJECT_NOT_READY",
        message: "当前作品尚未准备好，暂时无法核验运行状态。",
      });
      return;
    }
    void hydrateStatus({ token, projectId, chapterId, chapterKey });
  }, [chapter && chapter.id, chapter && chapter.backendId]); // eslint-disable-line react-hooks/exhaustive-deps

  const retryHydration = () => {
    const chapterId = chapter && chapter.backendId;
    const chapterKey = chapterId || (chapter && chapter.id) || "";
    const projectId = readyWorkId(WsWorks);
    if (!chapterId || !projectId) return;
    const token = requestRef.current + 1;
    requestRef.current = token;
    completedRef.current = null;
    statusRef.current = EMPTY_RUN.status;
    clearPoll();
    setRun(EMPTY_RUN);
    setCardOpen(false);
    void hydrateStatus({ token, projectId, chapterId, chapterKey });
  };

  const start = async () => {
    const chapterKey = (chapter && (chapter.backendId || chapter.id)) || "";
    const hydrationReady = hydration.status === "ready" && hydration.chapterKey === chapterKey;
    if (
      !hydrationReady
      || !chapter || chapter.current !== true || chapter.state === "approved"
      || submittingRef.current || confirmingRef.current || ACTIVE_STATUSES.has(run.status) || run.status === "completed"
    ) return;
    const chapterId = chapter && chapter.backendId;
    if (!chapterId) {
      setCardOpen(true);
      setRun({
        ...EMPTY_RUN,
        status: "failed",
        errorCode: "CHAPTER_NOT_SYNCED",
        message: "当前章节尚未同步到后端，等待自动保存完成后再运行。",
      });
      return;
    }
    const projectId = readyWorkId(WsWorks);
    if (!projectId) {
      setCardOpen(true);
      setRun({
        ...EMPTY_RUN,
        status: "failed",
        errorCode: "PROJECT_NOT_READY",
        message: "当前作品尚未准备好，请等待作品加载完成后再运行。",
      });
      return;
    }

    /* 整章批量起草是一件贵事：先说清楚要跑几场、会发生什么，再提交 */
    const sceneTotal = run.sceneCount || ((chapter && chapter.scenes) || []).length;
    const confirmToken = requestRef.current;
    confirmingRef.current = true;
    let ok = false;
    try {
      ok = await wsConfirm({
        title: "运行本章？",
        body: `会把本章${sceneTotal ? `的 ${sceneTotal} 场` : "的场"}按章节顺序交给 AI 逐一起草、过质检。任务在后台运行，会产生模型用量；跑完后去成稿中心审阅。`,
        confirmLabel: "运行本章",
      });
    } finally {
      confirmingRef.current = false;
    }
    // 确认期间换了章或卸载：这次确认作废
    if (!ok || !mountedRef.current || requestRef.current !== confirmToken) return;

    clearPoll();
    const token = requestRef.current + 1;
    requestRef.current = token;
    submittingRef.current = true;
    statusRef.current = "submitting";
    setCardOpen(true);
    setRun({ ...EMPTY_RUN, status: "submitting" });
    try {
      const result = await apiPost(
        `/api/v1/projects/${encodeURIComponent(projectId)}/chapters/${encodeURIComponent(chapterId)}/run-job`,
        {},
      );
      if (!isCurrent(token)) return;
      consume(result && result.run, { token, projectId, chapterId });
    } catch (error) {
      if (!isCurrent(token)) return;
      clearPoll();
      statusRef.current = "failed";
      setCardOpen(true);
      setRun({
        ...EMPTY_RUN,
        status: "failed",
        errorCode: (error && error.code) || "CHAPTER_RUN_START_FAILED",
        message: runErrorMessage(error),
      });
    } finally {
      if (isCurrent(token)) submittingRef.current = false;
    }
  };

  return { run, setRun, hydration, cardOpen, setCardOpen, start, retryHydration };
}
