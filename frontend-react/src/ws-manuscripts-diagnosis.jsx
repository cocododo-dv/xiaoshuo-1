import React from "react";
import { I } from "./icons.jsx";
import { apiGet, apiPost } from "./lib/client.js";
import { Notice, Spinner, Tag } from "./ws-ui.jsx";
import { wrAiError } from "./ws-writer-ai.js";
import { qSevLabel, qSevTone } from "./ws-quality-model.js";
import { announceDiagnosisChanged } from "./ws-diagnosis-summary.jsx";

/* ==========================================================
   成稿中心 · 诊断页签（2026-09-22 场景诊断统一）
   ----------------------------------------------------------
   读 GET /api/v1/chapters/{id}/deep-review：章级「AI 通读本章」的判断（承诺 / 升级 / 兑现 / 收束）、
   钉不到任何一场的章级发现，以及各场的诊断计数（写作台深改面板里的同一份，忽略过的不算）。
   「AI 通读本章」= POST 同一路径（拒绝式：无模型给「去系统设置」）；通读的发现钉得到哪一场就落到哪一场，
   在写作台那一场的深改面板里也是同一条（origin 通读）。每一场都能「去写作台看」，落到那一场的发现能
   「在写作台看这一处」（带 signal_id）。
   ========================================================== */

const { useEffect, useRef, useState } = React;

const AI_STATUS_TEXT = {
  not_run: "还没通读过：让模型读整章，判断承诺、升级、兑现与收束，发现落到各场。",
  current: "对着现在各场的正文。",
  stale: "这是改前的通读——之后有场改过；要按现在的稿子判断就重新通读。",
};

function useChapterDiagnosis(chapterId) {
  const [state, setState] = useState({ status: "idle", payload: null, error: null });
  const seq = useRef(0);
  const load = () => {
    if (!chapterId) { setState({ status: "idle", payload: null, error: null }); return Promise.resolve(); }
    const mine = ++seq.current;
    setState((prev) => ({ ...prev, status: "loading", error: null }));
    return apiGet(`/api/v1/chapters/${encodeURIComponent(chapterId)}/deep-review`)
      .then((payload) => { if (seq.current === mine) setState({ status: "ready", payload, error: null }); })
      .catch((error) => { if (seq.current === mine) setState((prev) => ({ ...prev, status: "error", error })); });
  };
  useEffect(() => {
    load();
    const onChanged = () => { load(); };
    window.addEventListener("ws:diagnosis-changed", onChanged);
    return () => { seq.current += 1; window.removeEventListener("ws:diagnosis-changed", onChanged); };
  }, [chapterId]); // eslint-disable-line react-hooks/exhaustive-deps
  return { ...state, reload: load, setPayload: (payload) => setState({ status: "ready", payload, error: null }) };
}

function DiagFinding({ finding, onLocate }) {
  return (
    <li className="ms-diag-row">
      <span className={`ms-diag-mark sev-${finding.severity}`} title={`严重程度：${qSevLabel(finding.severity)}`}>{finding.label || finding.dimension}</span>
      <span className="ms-diag-body">
        <span className="ms-diag-t">{finding.issue}</span>
        {finding.recommendation && <span className="ms-diag-fix">改法：{finding.recommendation}</span>}
        {finding.stale && <span className="ms-diag-stale">引的那句已经不在正文里</span>}
      </span>
      {onLocate && finding.evidence && (
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => onLocate(finding.signal_id)}>在写作台看这一处</button>
      )}
    </li>
  );
}

function ManuDiagnosis({ chapter, go }) {
  const chapterId = chapter && chapter.backendId;
  const { status, payload, error, reload, setPayload } = useChapterDiagnosis(chapterId);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState(null);
  const scenesById = {};
  ((chapter && chapter.scenes) || []).forEach((scene, index) => { if (scene && scene.backendId) scenesById[scene.backendId] = { ...scene, index }; });

  const run = async () => {
    if (!chapterId || running) return;
    setRunning(true);
    setRunError(null);
    try {
      const next = await apiPost(`/api/v1/chapters/${encodeURIComponent(chapterId)}/deep-review`, {});
      setPayload(next);
      announceDiagnosisChanged({ chapterId });
    } catch (err) {
      setRunError(err || new Error("chapter deep review failed"));
    } finally {
      setRunning(false);
    }
  };
  const goWriter = (scene, signalId) => {
    if (!go || !scene || !scene.sid) return;
    go("writer", [
      { type: "ws:writer-scene", detail: scene.sid },
      { type: "ws:writer-posture", detail: signalId ? { posture: "deep", signal_id: signalId } : "deep" },
    ]);
  };
  const openSettings = go ? () => go("settings", { type: "ws:settings-tab", detail: "ai" }) : null;

  if (!chapterId) {
    return <div className="ms-diag"><Notice tone="info">这一章还没有同步到服务器，暂时没有诊断。</Notice></div>;
  }
  const ai = (payload && payload.ai) || { status: "not_run" };
  const score = ai.overall_score != null ? Math.round(ai.overall_score * 100) : null;
  const when = ai.created_at ? new Date(ai.created_at) : null;
  const whenText = when && !Number.isNaN(when.getTime()) ? when.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  const brief = ai.revision_brief || [];
  const chapterFindings = (payload && payload.chapter_findings) || [];
  const scenes = (payload && payload.scenes) || [];
  const summary = (payload && payload.summary) || {};
  const errorInfo = runError ? wrAiError(runError) : null;

  return (
    <div className="ms-diag" data-testid="manuscript-diagnosis">
      <div className="card">
        <div className="card-head">
          <div className="card-title"><I.Sparkles size={14} /> AI 通读本章</div>
          <button type="button" className="btn btn-accent btn-sm" data-testid="chapter-deep-review-run" disabled={running || status === "loading"} onClick={run}>
            {running ? <Spinner size={13} /> : <I.Sparkles size={13} />} {running ? "通读中…" : (ai.status === "not_run" ? "AI 通读本章" : "重新通读")}
          </button>
        </div>
        <p className="ms-diag-meta">
          {ai.status !== "not_run" && whenText ? `${whenText}${score != null ? ` · 总分 ${score}` : ""}，` : ""}
          {AI_STATUS_TEXT[ai.status] || AI_STATUS_TEXT.not_run}
        </p>
        {errorInfo && (
          <Notice tone={errorInfo.kind === "config" ? "warn" : "danger"} className="ms-status"
            actions={<>
              {errorInfo.kind !== "config" && <button type="button" className="btn btn-ghost btn-sm" onClick={run}>{errorInfo.actionLabel}</button>}
              {errorInfo.offersSettings && openSettings && <button type="button" className="btn btn-ghost btn-sm" onClick={openSettings}>去系统设置</button>}
            </>}>
            {errorInfo.message}
          </Notice>
        )}
        {status === "error" && !payload && (
          <Notice tone="danger" className="ms-status" actions={<button type="button" className="btn btn-ghost btn-sm" onClick={reload}>重试</button>}>
            {(error && error.message) || "读不到本章的诊断。"}
          </Notice>
        )}
        {status === "loading" && !payload && <div className="ms-canonical-loading" role="status"><Spinner size={13} /> 正在读本章的诊断…</div>}
        {brief.length > 0 && ai.status !== "not_run" && (
          <ul className="ms-diag-brief">
            {brief.slice(0, 5).map((line, i) => <li key={i}>{line.action || line.fix_direction || line.recommendation || line.text || (line.target ? `${line.target}：${line.issue || ""}` : "")}</li>)}
          </ul>
        )}
        {ai.status !== "not_run" && chapterFindings.length > 0 && (
          <>
            <div className="ms-diag-sub">整章的判断 <span className="card-sub">{chapterFindings.length}</span></div>
            <ul className="ms-diag-list">{chapterFindings.map((f) => <DiagFinding key={f.signal_id} finding={f} />)}</ul>
          </>
        )}
        {ai.status !== "not_run" && chapterFindings.length === 0 && summary.open === 0 && (
          <p className="ms-diag-empty">通读没有留下待改的发现。</p>
        )}
      </div>

      {payload && (
        <div className="card">
          <div className="card-head">
            <div className="card-title">各场的诊断</div>
            <span className="card-sub">开着 {summary.open ?? 0} 条{summary.blocking ? ` · 阻断 ${summary.blocking}` : ""}</span>
          </div>
          {scenes.length === 0 ? <p className="ms-diag-empty">这一章还没有场。</p> : (
            <ul className="ms-diag-scenes">
              {scenes.map((entry, i) => {
                const catalogScene = scenesById[entry.scene_id] || null;
                const counts = entry.summary || {};
                const fromChapter = entry.findings_from_chapter || [];
                const title = (catalogScene && catalogScene.title) || entry.title || "";
                return (
                  <li key={entry.scene_id} className="ms-diag-scene">
                    <div className="ms-diag-scene-head">
                      <span className="ms-scene-idx">第 {catalogScene ? catalogScene.index + 1 : i + 1} 场</span>
                      <span className="ms-diag-scene-title">{title}</span>
                      {entry.text_layer === "none"
                        ? <span className="ms-diag-chip is-empty">没有正文</span>
                        : <span className={`ms-diag-chip ${counts.open ? "" : "is-clear"}`}>{counts.open ? `开着 ${counts.open}` : "没有待改"}{counts.by_severity && counts.by_severity.blocking ? ` · 阻断 ${counts.by_severity.blocking}` : ""}</span>}
                      {entry.ai_status === "current" && <span className="ms-diag-chip">深评过</span>}
                      {entry.ai_status === "stale" && <span className="ms-diag-chip is-stale">深评是改前的</span>}
                      {catalogScene && go && (
                        <button type="button" className="btn btn-quiet btn-sm ms-diag-go" onClick={() => goWriter(catalogScene)}>去写作台看</button>
                      )}
                    </div>
                    {fromChapter.length > 0 && (
                      <ul className="ms-diag-list">
                        {fromChapter.map((f) => <DiagFinding key={f.signal_id} finding={f} onLocate={catalogScene && go ? (signalId) => goWriter(catalogScene, signalId) : null} />)}
                      </ul>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export { ManuDiagnosis, useChapterDiagnosis };
