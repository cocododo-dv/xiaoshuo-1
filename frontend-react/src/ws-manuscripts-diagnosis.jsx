import React from "react";
import { I } from "./icons.jsx";
import { Notice, Spinner } from "./ws-ui.jsx";
import { wrAiError } from "./ws-writer-ai.js";
import { FindingLine } from "./ws-finding-ui.jsx";
import { writerIntents } from "./ws-view-intents.js";
import { useChapterDiagnosis } from "./ws-manuscripts-diagnosis-store.js";
import { formatLocaleMonthDayTime } from "./lib/format.js";

/* ==========================================================
   成稿中心 · 诊断页签（2026-09-22 场景诊断统一）
   ----------------------------------------------------------
   读 GET /api/v1/chapters/{id}/deep-review：章级「AI 通读本章」的判断（承诺 / 升级 / 兑现 / 收束）、
   钉不到任何一场的章级发现，以及各场的诊断计数（写作台深改面板里的同一份，忽略过的不算）。
   「AI 通读本章」= POST 同一路径（拒绝式：无模型给「去系统设置」）；通读的发现钉得到哪一场就落到哪一场，
   在写作台那一场的深改面板里也是同一条（origin 通读）。每一场都能「去写作台看」，落到那一场的发现能
   「在写作台看这一处」（带 signal_id）。
   第三轮：通读记着每场正文的哈希，改过的场服务端算得出来——改前的通读给「只通读改过的 N 场」（POST scope=changed：
   未改的场沿用上次的发现，标「沿用上次」）和「整章重新通读」；没有场改过时服务端不调模型，这里说一句。
   通读的响应带 diagnosis_rollup，随广播交给计数 store（WsDiagnosis）。读写都在 ws-manuscripts-diagnosis-store.js
   （一次通读只属于发起它的那一章：等模型时换了章，回包、提示、出错都不落到别的章上），
   这里只管画；一条发现的版式与文学质量共用（ws-finding-ui.jsx 的 FindingLine）。
   章里各场都还没有正文时（后端对它回 409 WRITER_DEEP_REVIEW_NO_TEXT，不调模型）两个通读按钮不可点。
   ========================================================== */

const AI_STATUS_TEXT = {
  not_run: "还没通读过：让模型读整章，判断承诺、升级、兑现与收束，发现落到各场。",
  current: "对着现在各场的正文。",
  stale: "这是改前的通读——之后有场改过；要按现在的稿子判断就重新通读。",
};

/* 「改过 2 场：第 3、5 场」——按目录里的场序说 */
function changedScenesText(ai, scenesById, entries) {
  const ids = (ai && ai.changed_scene_ids) || [];
  if (!ids.length) return "";
  const numbers = ids.map((id) => {
    const hit = scenesById[id];
    if (hit) return `第 ${hit.index + 1} 场`;
    const at = entries.findIndex((entry) => entry.scene_id === id);
    return at >= 0 ? `第 ${at + 1} 场` : "";
  }).filter(Boolean);
  return `改过 ${ids.length} 场${numbers.length ? `：${numbers.join("、")}` : ""}。`;
}

/* 各场都还没有正文（空白稿后端也报 text_layer "none"）：通读不了 */
function chapterHasNoText(payload) {
  return !!payload && (payload.scenes || []).every((entry) => entry && entry.text_layer === "none");
}

function ManuDiagnosis({ chapter, go }) {
  const chapterId = chapter && chapter.backendId;
  /* running：这一章正在跑的通读（"all" | "changed" | null）；runError / runNotice 也只是这一章的 */
  const { status, payload, error, reload, run, running, runError, runNotice } = useChapterDiagnosis(chapter);
  const scenesById = {};
  ((chapter && chapter.scenes) || []).forEach((scene, index) => { if (scene && scene.backendId) scenesById[scene.backendId] = { ...scene, index }; });

  const goWriter = (scene, signalId) => {
    if (!go || !scene || !scene.sid) return;
    go("writer", writerIntents(scene.sid, { deep: true, signalId: signalId || "" }));
  };
  const openSettings = go ? () => go("settings", { type: "ws:settings-tab", detail: "ai" }) : null;

  if (!chapterId) {
    return <div className="ms-diag"><Notice tone="info">这一章还没有同步到服务器，暂时没有诊断。</Notice></div>;
  }
  const ai = (payload && payload.ai) || { status: "not_run" };
  const score = ai.overall_score != null ? Math.round(ai.overall_score * 100) : null;
  const whenText = formatLocaleMonthDayTime(ai.created_at);
  const brief = ai.revision_brief || [];
  const chapterFindings = (payload && payload.chapter_findings) || [];
  const scenes = (payload && payload.scenes) || [];
  const summary = (payload && payload.summary) || {};
  const errorInfo = runError ? wrAiError(runError) : null;
  const busy = !!running || status === "loading";
  const noText = chapterHasNoText(payload);
  const noTextTip = noText ? "这一章的各场还没有正文，写出正文之后才能通读" : undefined;
  const incremental = ai.status === "stale" && !!ai.incremental_available && (ai.changed_count || 0) > 0;
  const changedText = ai.status === "stale" ? changedScenesText(ai, scenesById, scenes) : "";
  const scopeText = ai.status !== "not_run" && ai.scope === "changed" && (ai.carried_scene_ids || []).length
    ? `上次只通读了改过的 ${(ai.reviewed_scene_ids || []).length} 场，其余 ${(ai.carried_scene_ids || []).length} 场沿用更早的通读。`
    : "";

  return (
    <div className="ms-diag" data-testid="manuscript-diagnosis">
      <div className="card">
        <div className="card-head">
          <div className="card-title"><I.Sparkles size={14} /> AI 通读本章</div>
          <div className="ms-diag-acts">
            {incremental && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="chapter-deep-review-run-changed" disabled={busy || noText} onClick={() => run("changed")}
                title={noTextTip || "只把改过的场全文送审，没改的场沿用上次的发现"}>
                {running === "changed" ? <Spinner size={13} /> : <I.Sparkles size={13} />} {running === "changed" ? "通读中…" : `只通读改过的 ${ai.changed_count} 场`}
              </button>
            )}
            <button type="button" className={`btn ${incremental ? "btn-ghost" : "btn-accent"} btn-sm`} data-testid="chapter-deep-review-run" disabled={busy || noText} title={noTextTip} onClick={() => run("all")}>
              {running === "all" ? <Spinner size={13} /> : (incremental ? null : <I.Sparkles size={13} />)} {running === "all" ? "通读中…" : (ai.status === "not_run" ? "AI 通读本章" : (incremental ? "整章重新通读" : "重新通读"))}
            </button>
          </div>
        </div>
        <p className="ms-diag-meta">
          {ai.status !== "not_run" && whenText ? `${whenText}${score != null ? ` · 总分 ${score}` : ""}，` : ""}
          {AI_STATUS_TEXT[ai.status] || AI_STATUS_TEXT.not_run}
          {changedText ? ` ${changedText}` : ""}
          {scopeText ? ` ${scopeText}` : ""}
          {noText ? " 这一章的各场还没有正文，写出正文之后才能通读。" : ""}
        </p>
        {runNotice && <Notice tone="info" className="ms-status">{runNotice}</Notice>}
        {errorInfo && (
          <Notice tone={errorInfo.kind === "config" ? "warn" : "danger"} className="ms-status"
            actions={<>
              {errorInfo.kind !== "config" && <button type="button" className="btn btn-ghost btn-sm" onClick={() => run("all")}>{errorInfo.actionLabel}</button>}
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
            <ul className="ms-diag-list">{chapterFindings.map((f) => <FindingLine key={f.signal_id} layout="row" finding={f} />)}</ul>
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
                      {entry.changed_since_review && <span className="ms-diag-chip is-stale">通读后改过</span>}
                      {entry.carried && !entry.changed_since_review && <span className="ms-diag-chip is-empty">沿用上次通读</span>}
                      {catalogScene && go && (
                        <button type="button" className="btn btn-quiet btn-sm ms-diag-go" onClick={() => goWriter(catalogScene)}>去写作台看</button>
                      )}
                    </div>
                    {fromChapter.length > 0 && (
                      <ul className="ms-diag-list">
                        {fromChapter.map((f) => <FindingLine key={f.signal_id} layout="row" finding={f} onLocate={catalogScene && go ? (signalId) => goWriter(catalogScene, signalId) : null} />)}
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

export { ManuDiagnosis };
