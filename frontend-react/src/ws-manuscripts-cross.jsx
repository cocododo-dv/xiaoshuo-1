import React from "react";
import { I } from "./icons.jsx";
import { Notice, Spinner } from "./ws-ui.jsx";
import { apiGet } from "./lib/client.js";
import { useWindowEvents } from "./lib/events.js";
import { findSceneByBackendId } from "./labels/catalog.js";
import { writerIntents } from "./ws-view-intents.js";

/* ==========================================================
   成稿中心 · 跨章页签（2026-10-20）
   ----------------------------------------------------------
   GET /api/v1/projects/{id}/cross-chapter-check：把全书已写的章放在一起看（不用模型，按各场现在的正文现算，读不写）：
   · 反复出现的写法——同一个说法在不同的场里逐字出现；每一处也是写作台深改里那一场的一条「跨场重复」（同一个
     signal_id），点哪一场就进写作台那一场、停在那一条上（能改写、能忽略）；在某一场忽略过的那一处标「已忽略」；
   · 章与章——写完的章连着几章停在同一种收尾、以同一种开头起，篇幅异常的章（至少三章写完才比）；
   · 线索——线索台账里到了计划兑现的章还没兑现、好几章没碰的线（标记与处理在「记忆」页签：onOpenMemory）。
   别处的诊断变了（写作台保存、忽略）就重读。
   ========================================================== */

const { useCallback, useEffect, useRef, useState } = React;

const crossPath = (projectId) => `/api/v1/projects/${encodeURIComponent(projectId)}/cross-chapter-check`;

function useCrossCheck(projectId) {
  const [state, setState] = useState({ status: "idle", payload: null, error: null, projectId: "" });
  const seq = useRef(0);
  const load = useCallback(() => {
    const mine = ++seq.current;
    if (!projectId) { setState({ status: "idle", payload: null, error: null, projectId: "" }); return; }
    setState((prev) => (prev.projectId === projectId
      ? { ...prev, status: "loading", error: null }
      : { status: "loading", payload: null, error: null, projectId }));
    apiGet(crossPath(projectId))
      .then((payload) => { if (seq.current === mine) setState({ status: "ready", payload, error: null, projectId }); })
      .catch((error) => { if (seq.current === mine) setState((prev) => ({ ...prev, status: "error", error })); });
  }, [projectId]);
  useEffect(() => {
    load();
    return () => { seq.current += 1; };
  }, [load]);
  useWindowEvents({ "ws:diagnosis-changed": () => load() });
  return { ...state, reload: load };
}

const place = (chapterNumber, sceneNumber) => `第 ${chapterNumber} 章第 ${sceneNumber} 场`;
const chapterList = (numbers) => numbers.map((n) => `第 ${n} 章`).join("、");

function RepeatItem({ item, chapters, go }) {
  const open = (occurrence) => {
    const hit = findSceneByBackendId(chapters, occurrence.scene_id);
    if (!go || !hit || !hit.scene || !hit.scene.sid) return;
    go("writer", writerIntents(hit.scene.sid, { signalId: item.signal_id }));
  };
  return (
    <li className="ms-diag-scene" data-testid="cross-repeat">
      <div className="ms-diag-scene-head">
        <span className="ms-diag-scene-title">「{item.phrase}」</span>
        <span className="ms-diag-chip">{item.count} 次 · {item.scene_count} 场</span>
        {item.habitual && <span className="ms-diag-chip is-empty" title={`参考书里 ${item.reference_count} 次`}>参考作者也常这么写</span>}
        {item.ignored && <span className="ms-diag-chip is-clear">都已忽略</span>}
      </div>
      <ul className="ms-diag-list">
        {item.occurrences.map((occurrence, index) => (
          <li key={`${occurrence.scene_id}:${occurrence.paragraph_index}:${index}`} className="ms-diag-row">
            <span className="ms-diag-mark sev-taste">{place(occurrence.chapter_number, occurrence.scene_number)}</span>
            <span className="ms-diag-body">
              <span className="ms-diag-fix">{occurrence.excerpt}</span>
              {occurrence.ignored && <span className="ms-diag-carried">这一处已忽略</span>}
            </span>
            {go && (
              <button type="button" className="btn btn-quiet btn-sm" data-testid="cross-repeat-go" onClick={() => open(occurrence)}>
                在写作台看这一处
              </button>
            )}
          </li>
        ))}
      </ul>
    </li>
  );
}

function ShapesCard({ shapes }) {
  const streaks = (shapes && shapes.streaks) || [];
  const lengths = (shapes && shapes.lengths) || { items: [], enough: false };
  const complete = ((shapes && shapes.chapters) || []).filter((chapter) => chapter.complete).length;
  const min = (shapes && shapes.streak_min_chapters) || 3;
  return (
    <div className="card" data-testid="cross-shapes">
      <div className="card-head">
        <div className="card-title">章与章</div>
        <span className="card-sub">写完 {complete} 章</span>
      </div>
      {complete < min ? (
        <p className="ms-diag-empty">要至少 {min} 章写完（每一场都有正文）才看得出章与章之间的形状。</p>
      ) : (
        <>
          {streaks.length === 0 && lengths.items.length === 0 && <p className="ms-diag-empty">没有连着几章同样收尾或同样开头的，篇幅也没有特别长短的章。</p>}
          {streaks.length > 0 && (
            <ul className="ms-diag-list">
              {streaks.map((streak) => (
                <li key={`${streak.edge}:${streak.chapter_ids.join(",")}`} className="ms-diag-row" data-testid="cross-streak">
                  <span className="ms-diag-mark sev-taste">{streak.edge === "ending" ? "收尾" : "开头"}</span>
                  <span className="ms-diag-body">
                    <span className="ms-diag-t">
                      {chapterList(streak.chapter_numbers)}连着 {streak.chapter_numbers.length} 章都{streak.edge === "ending" ? "停在" : "以"}{streak.label}{streak.edge === "ending" ? "上" : "起"}。
                    </span>
                    <span className="ms-diag-fix">读者会摸到规律；换一两章的{streak.edge === "ending" ? "收法" : "起法"}。</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {lengths.items.length > 0 && (
            <ul className="ms-diag-list">
              {lengths.items.map((item) => (
                <li key={item.chapter_id} className="ms-diag-row" data-testid="cross-length">
                  <span className="ms-diag-mark sev-info">篇幅</span>
                  <span className="ms-diag-body">
                    <span className="ms-diag-t">
                      第 {item.number} 章 {item.chars.toLocaleString("zh-CN")} 字，是写完各章中位数（{(lengths.median_chars || 0).toLocaleString("zh-CN")} 字）的 {item.ratio} 倍{item.kind === "short" ? "，偏短" : "，偏长"}。
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

function ThreadsCard({ threads, onOpenMemory }) {
  const t = threads || { open_count: 0, payoff_missed: 0, cold: 0, items: [] };
  return (
    <div className="card" data-testid="cross-threads">
      <div className="card-head">
        <div className="card-title">线索</div>
        <span className="card-sub">还开着 {t.open_count} 条</span>
        {onOpenMemory && <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenMemory}>去「记忆」页签</button>}
      </div>
      {t.payoff_missed + t.cold === 0 ? (
        <p className="ms-diag-empty">线索台账里没有该兑现还没兑现、或好几章没碰的线。台账由每章写完后的章摘要记下。</p>
      ) : (
        <ul className="ms-diag-list">
          {t.items.map((item) => (
            <li key={item.thread_id} className="ms-diag-row">
              <span className={`ms-diag-mark ${item.alert === "payoff_missed" ? "sev-revision" : "sev-taste"}`}>{item.alert === "payoff_missed" ? "该兑现了" : "好久没碰"}</span>
              <span className="ms-diag-body">
                <span className="ms-diag-t">{item.thread}</span>
                <span className="ms-diag-fix">
                  第 {item.opened_in} 章埋下，最近一次在第 {item.last_touched_in} 章{item.planned_payoff_in ? `，计划在第 ${item.planned_payoff_in} 章兑现` : ""}。
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ManuCrossCheck({ projectId, chapters = [], go, onOpenMemory }) {
  const { status, payload, error, reload } = useCrossCheck(projectId);
  if (!projectId) return <div className="ms-diag"><Notice tone="info">先选一部作品。</Notice></div>;
  const repeats = (payload && payload.repeats) || [];
  return (
    <div className="ms-diag" data-testid="manuscript-cross">
      <div className="card">
        <div className="card-head">
          <div className="card-title"><I.Layers size={14} /> 反复出现的写法</div>
          <div className="ms-diag-acts">
            <button type="button" className="btn btn-ghost btn-sm" disabled={status === "loading"} onClick={reload} data-testid="cross-reload">
              {status === "loading" ? <Spinner size={13} /> : <I.Refresh size={13} />} 重新检查
            </button>
          </div>
        </div>
        <p className="ms-diag-meta">
          {payload ? `全书已写 ${payload.chapters_with_text} 章 ${payload.scenes_with_text} 场、${Number(payload.chars || 0).toLocaleString("zh-CN")} 字。` : ""}
          同一个说法在不同的场里逐字出现，读者会注意到它。不用模型，按各场现在的正文算；每一处也在写作台深改的清单里（来源「跨场重复」），能改写、能忽略。
          {payload && payload.reference_bound ? "参考作者也常这么写的只作提示。" : ""}
        </p>
        {status === "error" && !payload && (
          <Notice tone="danger" className="ms-status" actions={<button type="button" className="btn btn-ghost btn-sm" onClick={reload}>重试</button>}>
            {(error && error.message) || "读不到跨章检查。"}
          </Notice>
        )}
        {status === "loading" && !payload && <div className="ms-canonical-loading" role="status"><Spinner size={13} /> 正在把全书放在一起看…</div>}
        {payload && repeats.length === 0 && <p className="ms-diag-empty">没有在不同场里反复出现的写法。</p>}
        {repeats.length > 0 && (
          <ul className="ms-diag-scenes">
            {repeats.map((item) => <RepeatItem key={item.signal_id} item={item} chapters={chapters} go={go} />)}
          </ul>
        )}
      </div>
      {payload && <ShapesCard shapes={payload.shapes} />}
      {payload && <ThreadsCard threads={payload.threads} onOpenMemory={onOpenMemory} />}
    </div>
  );
}

export { ManuCrossCheck };
