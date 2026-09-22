import React from "react";
import { Spinner } from "./ws-ui.jsx";
import { cancelRunJob, getLatestSceneRunJob } from "./lib/client.js";
import {
  RUN_JOB_CANCELABLE_STATUSES, RUN_JOB_POLLING_STATUSES, RUN_JOB_STATUS_LABELS, RUN_JOB_TERMINAL_STATUSES,
  runJobStepLabel,
} from "./ws-scene-derive.js";

const { useCallback, useEffect, useRef, useState } = React;

/* 读任务 / 取消任务失败时给作者看的那句话。错误码只放进 data-code（排查用），不念给作者：
   过去这里是「RUN_JOB_CANCEL_CONFLICT · terminal scene run job cannot be cancelled · job_id: … · status: completed」。 */
function runJobErrorNotice(error, action) {
  const code = error && error.code ? String(error.code) : "REQUEST_FAILED";
  const details = error && error.details && typeof error.details === "object" ? error.details : {};
  if (action === "cancel" && error && error.status === 409 && details.status) {
    return { code, text: `任务已经是「${RUN_JOB_STATUS_LABELS[details.status] || "结束"}」，不能再取消。` };
  }
  const message = error && error.message ? String(error.message) : "请求失败";
  return action === "cancel"
    ? { code, text: `取消没有成功：${message}。可以再试一次。` }
    : { code, text: `读不到这一场的运行任务：${message}。` };
}

function isRunJobStateRegression(currentJob, nextJob) {
  if (!currentJob || !nextJob || currentJob.job_id !== nextJob.job_id) return false;
  if (RUN_JOB_TERMINAL_STATUSES.has(currentJob.status)) {
    return currentJob.status !== nextJob.status;
  }
  if (currentJob.status === "cancel_requested") {
    return nextJob.status === "queued" || nextJob.status === "running";
  }
  return currentJob.status === "running" && nextJob.status === "queued";
}

/* 场景运行任务控制条：唯一的轮询者（startRun 靠它报来的终态收尾，不再自己另起一个轮询）。
   fetchLatest=false：这一场从没进过管线（场景页已从 /scene-run-states 得知），不去问 latest——
   否则每点开一场没跑过的戏，浏览器控制台都多一条 404。POST 建出的任务仍经 observedJob 接上并照常轮询。 */
function SceneRunJobControl({
  sceneId,
  observedJob = null,
  onJobChange = null,
  pollIntervalMs = 2000,
  refreshSignal = 0,
  draftMode = null,
  fetchLatest = true,
}) {
  const [job, setJob] = useState(null);
  const [loading, setLoading] = useState(Boolean(sceneId));
  const [cancelling, setCancelling] = useState(false);
  const [errorNotice, setErrorNotice] = useState(null);   // { code, text }
  const jobRef = useRef(null);
  const sceneRef = useRef(sceneId || "");
  const epochRef = useRef(0);
  const requestVersionRef = useRef(0);
  const cancelInFlightRef = useRef(false);
  const refreshInFlightRef = useRef(false);
  const refreshAbortRef = useRef(null);
  const cancelAbortRef = useRef(null);
  const onJobChangeRef = useRef(onJobChange);

  useEffect(() => {
    onJobChangeRef.current = onJobChange;
  }, [onJobChange]);

  const publishJob = useCallback((nextJob, epoch = epochRef.current) => {
    if (epoch !== epochRef.current) return false;
    const expectedSceneId = sceneRef.current;
    if (
      nextJob
      && nextJob.scene_id
      && expectedSceneId
      && String(nextJob.scene_id) !== String(expectedSceneId)
    ) {
      return false;
    }
    if (isRunJobStateRegression(jobRef.current, nextJob)) return false;
    jobRef.current = nextJob || null;
    setJob(nextJob || null);
    if (onJobChangeRef.current) onJobChangeRef.current(nextJob || null);
    return true;
  }, []);

  const refreshLatest = useCallback(async ({ silent = false, epoch = epochRef.current, force = false } = {}) => {
    const targetSceneId = sceneRef.current;
    if (!targetSceneId || epoch !== epochRef.current || (refreshInFlightRef.current && !force)) return null;
    if (force && refreshAbortRef.current) refreshAbortRef.current.abort();
    const requestVersion = requestVersionRef.current + 1;
    requestVersionRef.current = requestVersion;
    const controller = new AbortController();
    refreshAbortRef.current = controller;
    refreshInFlightRef.current = true;
    if (!silent) setLoading(true);
    try {
      const latest = await getLatestSceneRunJob(targetSceneId, { signal: controller.signal });
      if (requestVersion !== requestVersionRef.current) return null;
      if (publishJob(latest, epoch) && !silent) setErrorNotice(null);
      return latest;
    } catch (error) {
      if (epoch !== epochRef.current || requestVersion !== requestVersionRef.current) return null;
      if (error && (error.status === 404 || error.code === "RUN_JOB_NOT_FOUND")) {
        publishJob(null, epoch);
        if (!silent) setErrorNotice(null);
        return null;
      }
      if (!silent) setErrorNotice(runJobErrorNotice(error, "refresh"));
      return null;
    } finally {
      if (refreshAbortRef.current === controller) refreshAbortRef.current = null;
      if (epoch === epochRef.current && requestVersion === requestVersionRef.current) {
        refreshInFlightRef.current = false;
        if (!silent) setLoading(false);
      }
    }
  }, [publishJob]);

  useEffect(() => {
    const epoch = epochRef.current + 1;
    epochRef.current = epoch;
    requestVersionRef.current += 1;
    if (refreshAbortRef.current) refreshAbortRef.current.abort();
    if (cancelAbortRef.current) cancelAbortRef.current.abort();
    refreshAbortRef.current = null;
    cancelAbortRef.current = null;
    sceneRef.current = sceneId || "";
    refreshInFlightRef.current = false;
    cancelInFlightRef.current = false;
    jobRef.current = null;
    setCancelling(false);
    setErrorNotice(null);
    publishJob(null, epoch);
    if (!sceneId || !fetchLatest) {
      setLoading(false);
      return () => {
        if (epochRef.current === epoch) epochRef.current += 1;
      };
    }
    void refreshLatest({ epoch });
    return () => {
      if (epochRef.current === epoch) epochRef.current += 1;
      if (refreshAbortRef.current) refreshAbortRef.current.abort();
      if (cancelAbortRef.current) cancelAbortRef.current.abort();
      refreshAbortRef.current = null;
      cancelAbortRef.current = null;
      refreshInFlightRef.current = false;
      cancelInFlightRef.current = false;
    };
    // fetchLatest 只决定挂载 / 换场时要不要问 latest；它中途变成 true（这一场刚进了在办）由下一个 effect 补问，
    // 不在这里重置——重置会把已经接上的 POST 任务清掉。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sceneId, publishJob, refreshLatest]);

  const fetchLatestRef = useRef(fetchLatest);
  useEffect(() => {
    const was = fetchLatestRef.current;
    fetchLatestRef.current = fetchLatest;
    if (!sceneId || !fetchLatest || was) return;
    void refreshLatest({ silent: Boolean(jobRef.current) });
  }, [fetchLatest, sceneId, refreshLatest]);

  useEffect(() => {
    if (!observedJob || !sceneId) return;
    // POST 的回包比任何在途的 latest 都新
    requestVersionRef.current += 1;
    if (refreshAbortRef.current) refreshAbortRef.current.abort();
    refreshAbortRef.current = null;
    refreshInFlightRef.current = false;
    publishJob(observedJob);
  }, [observedJob, sceneId, publishJob]);

  /* 归档等页面动作后由父组件递增 refreshSignal：终态 job 不轮询，
     不刷新的话横幅会停留在旧暂停点（如 awaiting_candidate_selection）。 */
  useEffect(() => {
    if (!sceneId || !refreshSignal) return;
    void refreshLatest({ silent: true, force: true });
  }, [refreshSignal, sceneId, refreshLatest]);

  useEffect(() => {
    if (!sceneId || !job || !RUN_JOB_POLLING_STATUSES.has(job.status)) return undefined;
    const timer = window.setInterval(() => {
      void refreshLatest({ silent: true });
    }, Math.max(1, pollIntervalMs));
    return () => window.clearInterval(timer);
  }, [sceneId, job && job.job_id, job && job.status, pollIntervalMs, refreshLatest]);

  const requestCancellation = useCallback(async () => {
    const currentJob = job;
    if (
      !currentJob
      || !RUN_JOB_CANCELABLE_STATUSES.has(currentJob.status)
      || cancelInFlightRef.current
    ) {
      return;
    }
    const epoch = epochRef.current;
    requestVersionRef.current += 1;
    if (refreshAbortRef.current) refreshAbortRef.current.abort();
    refreshAbortRef.current = null;
    refreshInFlightRef.current = false;
    cancelInFlightRef.current = true;
    const controller = new AbortController();
    cancelAbortRef.current = controller;
    setCancelling(true);
    setErrorNotice(null);
    try {
      const nextJob = await cancelRunJob(currentJob.job_id, { signal: controller.signal });
      if (epoch !== epochRef.current) return;
      if (!jobRef.current || jobRef.current.job_id !== currentJob.job_id) {
        await refreshLatest({ silent: true, epoch, force: true });
        return;
      }
      requestVersionRef.current += 1;
      refreshInFlightRef.current = false;
      publishJob(nextJob, epoch);
    } catch (error) {
      if (epoch !== epochRef.current) return;
      if (
        !jobRef.current
        || jobRef.current.job_id !== currentJob.job_id
        || !RUN_JOB_CANCELABLE_STATUSES.has(jobRef.current.status)
      ) {
        await refreshLatest({ silent: true, epoch, force: true });
        return;
      }
      setErrorNotice(runJobErrorNotice(error, "cancel"));
      if (error && error.status === 409) {
        await refreshLatest({ silent: true, epoch, force: true });
      }
    } finally {
      if (cancelAbortRef.current === controller) cancelAbortRef.current = null;
      if (epoch === epochRef.current) {
        cancelInFlightRef.current = false;
        setCancelling(false);
      }
    }
  }, [job, publishJob, refreshLatest]);

  if (!sceneId) return null;

  const status = job && job.status ? job.status : "none";
  const statusLabel = loading && !job
    ? "正在恢复运行任务"
    : (RUN_JOB_STATUS_LABELS[status] || (job ? status : "暂无运行任务"));
  const showCancel = Boolean(job && RUN_JOB_CANCELABLE_STATUSES.has(status));
  const showCancelling = Boolean(job && status === "cancel_requested");
  const stepLabel = job && job.current_step ? runJobStepLabel(job.current_step, (job && job.draft_mode) || draftMode) : "";

  return (
    <div
      className={`scn2-job${job ? "" : " is-quiet"}`}
      data-testid="scene-run-job-control"
      data-status={status}
      data-job-id={(job && job.job_id) || ""}
    >
      <span className="scn2-job-sum" role="status" aria-live="polite" aria-atomic="true">
        {RUN_JOB_POLLING_STATUSES.has(status) && <Spinner size={12} className="scn2-job-spin" />}
        <span>
          {statusLabel}
          {stepLabel && stepLabel !== statusLabel ? ` · ${stepLabel}` : ""}
        </span>
      </span>
      <span className="scn2-job-acts">
        {errorNotice && (
          <span className="scn2-job-err" role="alert" data-testid="scene-run-cancel-error" data-code={errorNotice.code}>
            {errorNotice.text}
          </span>
        )}
        {showCancel && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-testid="scene-run-cancel-button"
            disabled={cancelling}
            aria-disabled={cancelling ? "true" : "false"}
            onClick={requestCancellation}
          >
            {cancelling ? "正在提交取消…" : "取消运行"}
          </button>
        )}
        {showCancelling && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-testid="scene-run-cancel-button"
            disabled
            aria-disabled="true"
          >
            取消处理中
          </button>
        )}
      </span>
    </div>
  );
}

export { SceneRunJobControl };
