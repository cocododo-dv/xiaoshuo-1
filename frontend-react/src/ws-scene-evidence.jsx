import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { CloseButton, Tag } from "./ws-ui.jsx";
import { scnFetchStyleWindowText } from "./ws-scene-api.js";
import {
  STYLE_WINDOW_STEP_LABELS, scnFindingIsPlainLanguage, scnFindingText, scnStyleWindowKey, scnStyleWindowLabel,
  scnStyleWindowTags,
} from "./ws-scene-derive.js";
import { SceneFidelityPanel } from "./ws-scene-fidelity.jsx";

const { useEffect, useRef, useState } = React;

/* ==========================================================
   AI 起草台 — 证据栏（右栏；≤1120px 变成从右侧拉出的抽屉）
   后端裁决 · 像不像 · 本场参考窗口 · 尝试历史（可开复盘）· 运行记录 · 本次运行。
   没东西可看时整栏收起（页面按 hasEvidence 决定挂不挂）。
   ========================================================== */

const GATE_SUMMARY = {
  draft_ready: "后端质检没有提出问题，可以归档。",
  quality_warning: "有质量建议，可以直接归档，建议随稿留痕。",
  hard_blocked: "有已证实的硬问题，这一稿暂不能归档。",
  awaiting_author_choice: "关键场景写出了几份候选稿，等你终选。",
  archived: "已归档。",
};

/* 后端裁决：带中文说明的条目逐条列出；原样透出的英文异常折进「查看原文」；没附说明的只计数。 */
function GateBlock({ gate, budgetBlock }) {
  const findings = [
    ...(gate.blocking || []).map(f => ({ tone: "danger", text: scnFindingText(f) })),
    ...(gate.warnings || []).map(f => ({ tone: "warn", text: scnFindingText(f) })),
  ];
  const readable = findings.filter(f => scnFindingIsPlainLanguage(f.text));
  const raw = findings.filter(f => f.text && !scnFindingIsPlainLanguage(f.text));
  const unexplained = findings.length - readable.length - raw.length;
  const summary = budgetBlock
    ? `${budgetBlock.label}，追加后才能继续。`
    : (GATE_SUMMARY[gate.authorState] || (gate.canArchive === false ? "后端暂不允许归档这一稿。" : "可以归档。"));
  return (
    <section className="scn2-evi-block" data-testid="scene-gate">
      <h3 className="scn2-evi-h"><I.ShieldCheck size={13} /> 后端裁决</h3>
      <p className="scn2-gate-sum">{summary}</p>
      {readable.length > 0 && (
        <ul className="scn2-gate-list">
          {readable.map((f, i) => <li key={i} data-tone={f.tone}>{f.text}</li>)}
        </ul>
      )}
      {unexplained > 0 && <p className="scn2-gate-more">另有 {unexplained} 条没有附说明。</p>}
      {raw.length > 0 && (
        <details className="scn2-gate-raw">
          <summary>另有 {raw.length} 条只有后端原始信息，查看原文</summary>
          {raw.map((f, i) => <pre key={i}>{f.text}</pre>)}
        </details>
      )}
    </section>
  );
}

/* 本场参考窗口：这一场提示里实际放入的参考书原文窗口——在哪一章、章首还是章末、多长，学习文风时给它打的一句话梗概
   与场面 / 情绪 / 手法标签、按哪条配额选进来的（v3 的窗才有后几样）。默认收起，展开时才按区间取原文并在组件内缓存；
   参考书已不可用（没有 bookId）时整行不可展开。 */
function SceneStyleWindowsPanel({ styleWindows, fetchText = scnFetchStyleWindowText }) {
  const [open, setOpen] = useState({});
  const [texts, setTexts] = useState({});
  const [loading, setLoading] = useState({});
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);
  const windows = styleWindows && Array.isArray(styleWindows.windows) ? styleWindows.windows : [];
  if (!windows.length) return null;
  const bookId = styleWindows.bookId || null;
  const stepLabel = styleWindows.step ? (STYLE_WINDOW_STEP_LABELS[styleWindows.step] || styleWindows.step) : "";
  const toggle = async (w) => {
    const key = scnStyleWindowKey(bookId, w);
    const next = !open[key];
    setOpen((m) => ({ ...m, [key]: next }));
    const cached = texts[key];
    if (!next || !bookId || loading[key] || (cached && !cached.error)) return;
    setLoading((m) => ({ ...m, [key]: true }));
    try {
      const data = await fetchText(bookId, w);
      const paragraphs = Array.isArray(data && data.paragraphs)
        ? data.paragraphs.filter((p) => p && typeof p.text === "string")
        : [];
      if (aliveRef.current) setTexts((m) => ({ ...m, [key]: { paragraphs, capped: Boolean(data && data.capped) } }));
    } catch (e) {
      if (aliveRef.current) setTexts((m) => ({ ...m, [key]: { error: (e && e.message) || "原文取回失败，请重试" } }));
    } finally {
      if (aliveRef.current) setLoading((m) => ({ ...m, [key]: false }));
    }
  };
  return (
    <section className="scn2-evi-block scn2-style-windows" data-testid="scene-style-windows">
      <h3 className="scn2-evi-h"><I.BookOpen size={13} /> 本场参考窗口 · {windows.length}</h3>
      <p className="scn2-style-windows-hint">
        这一场提示里实际放入的参考书原文窗口{stepLabel ? `（${stepLabel}）` : ""}，展开可核对{bookId ? "" : "；参考书已不可用，无法展开原文"}。
      </p>
      <ul className="scn2-style-window-list">
        {windows.map((w, i) => {
          const key = scnStyleWindowKey(bookId, w);
          const isOpen = Boolean(open[key]);
          const entry = texts[key];
          const busy = Boolean(loading[key]);
          const panelId = `scn2-style-window-${i}`;
          const labels = scnStyleWindowTags(w);
          const hasTags = !!(labels.slot || labels.tags.length || labels.devices.length || labels.paragraphType);
          return (
            <li key={key} className={`scn2-style-window${isOpen ? " is-open" : ""}`} data-testid="scene-style-window-row">
              <button
                type="button"
                className="scn2-style-window-row"
                aria-expanded={isOpen ? "true" : "false"}
                aria-controls={panelId}
                disabled={!bookId}
                onClick={() => toggle(w)}
              >
                {isOpen ? <I.ChevronDown size={12} /> : <I.ChevronRight size={12} />}
                <span className="scn2-style-window-main">
                  <span className="scn2-style-window-label">{scnStyleWindowLabel(w)}</span>
                  {w.gist && <span className="scn2-style-window-gist text-serif" data-testid="scene-style-window-gist">{w.gist}</span>}
                  {hasTags && (
                    <span className="scn2-style-window-tags">
                      {labels.slot && <Tag outline>{labels.slot}</Tag>}
                      {labels.tags.map((tag) => <Tag key={`t-${tag}`}>{tag}</Tag>)}
                      {labels.devices.map((device) => <Tag key={`d-${device}`} tone="accent" outline>{device}</Tag>)}
                      {labels.paragraphType && <Tag outline>{labels.paragraphType}</Tag>}
                    </span>
                  )}
                </span>
              </button>
              {isOpen && (
                <div id={panelId} className="scn2-style-window-text" data-testid="scene-style-window-text">
                  {busy && <span className="scn2-style-window-status">正在取回原文…</span>}
                  {entry && entry.error && <span className="scn2-style-window-status is-error" role="alert">{entry.error}（再点一次重试）</span>}
                  {entry && entry.paragraphs && (entry.paragraphs.length
                    ? entry.paragraphs.map((p) => (
                        <p key={p.paragraph_index}>
                          <span className="scn2-style-window-idx tab-num">{Number.isInteger(p.paragraph_index) ? p.paragraph_index + 1 : ""}</span>
                          {p.text}
                        </p>
                      ))
                    : <span className="scn2-style-window-status">这段区间已没有可显示的段落（参考书可能已重新导入）</span>)}
                  {entry && entry.capped && <span className="scn2-style-window-status">只显示了这一窗的前 80 段</span>}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

const ATTEMPT_TONE = { gold: "warn", sage: "ok", rose: "danger", crimson: "accent", slate: "info" };
const attemptResultLabel = (attempt) => (attempt.result === "running" ? "进行中" : attempt.result);

function Evidence({ scene, sceneId = null, go = null, state, open, onClose, logOpen, setLogOpen, onView }) {
  const log = scene.log || [];
  const cost = scene.cost || [];
  return (
    <aside id="scn2-evi" className={`scn2-evi scn2-scroll${open ? " is-open" : ""}`} aria-label="证据">
      <div className="scn2-evi-drawerhead">
        <span>证据</span>
        <CloseButton label="收起证据" onClick={onClose} />
      </div>

      {(state === "ready" || state === "archived") && scene.gate && <GateBlock gate={scene.gate} budgetBlock={scene.budgetBlock} />}

      <SceneFidelityPanel sceneId={sceneId} runFidelity={scene.styleFidelity || null} go={go} />

      <SceneStyleWindowsPanel styleWindows={scene.styleWindows} />

      {scene.attempts && scene.attempts.length > 0 && (
        <section className="scn2-evi-block">
          <h3 className="scn2-evi-h"><I.Clock size={13} /> 尝试历史 · {scene.attempts.length}</h3>
          <ul className="scn2-tries">
            {scene.attempts.map((a, i) => (
              <li key={i} className={`scn2-try ${i === 0 ? "is-current" : ""}`}>
                <span className="scn2-try-n tab-num">#{a.n}</span>
                <span className="scn2-try-body">
                  <span className="scn2-try-top">
                    <span className="scn2-try-time">{a.time}</span>
                    <span className={`scn2-try-tag tone-${a.tone}`}>{attemptResultLabel(a)}</span>
                  </span>
                  <span className="scn2-try-note">{a.note}</span>
                </span>
                {i !== 0 && <button type="button" className="scn2-try-view" onClick={() => onView(a)} aria-label={`查看第 ${a.n} 次尝试的复盘`}>复盘</button>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {log.length > 0 && (
        <section className="scn2-evi-block">
          <button type="button" className="scn2-evi-fold" aria-expanded={logOpen} aria-controls="scn2-run-log" onClick={() => setLogOpen(o => !o)}>
            <I.Activity size={13} /><span>运行记录</span><span className="scn2-evi-count tab-num">{log.length}</span>
            <I.ChevronRight size={13} className="scn2-evi-caret" />
          </button>
          {logOpen && (
            <ol id="scn2-run-log" className="scn2-log">
              {log.map((l, i) => (
                <li key={i} className="scn2-log-row">
                  <span className="scn2-log-t tab-num">{l.t}</span>
                  <span className="scn2-log-text">{l.text}</span>
                </li>
              ))}
            </ol>
          )}
        </section>
      )}

      {cost.length > 0 && (
        <section className="scn2-evi-block">
          <h3 className="scn2-evi-h"><I.Coins size={13} /> 本次运行</h3>
          <ul className="scn2-rows">
            {cost.map((c, i) => (
              <li key={i}><span>{c.k}</span><strong className={c.mono ? "tab-num" : ""}>{c.v}</strong></li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}

/* 尝试复盘：只有这一次尝试留下的复盘摘要；重写永远以当前稿为输入。
   留在场景页的 DOM 里（portal={false}）——它说的是这一场，测试也在容器内查它。 */
function AttemptCompare({ attempt, onClose, onRewrite }) {
  const titleId = React.useId();
  const cmp = attempt.cmp || {};
  return (
    <WsDialog portal={false} onClose={onClose} labelledBy={titleId} size="md" className="scn2-cmp">
      <div className="ws-dialog-head">
        <div className="scn2-cmp-title">
          <h2 className="ws-dialog-title" id={titleId}>第 {attempt.n} 次尝试</h2>
          <div className="scn2-cmp-meta">
            <Tag tone={ATTEMPT_TONE[attempt.tone] || "neutral"}>{attemptResultLabel(attempt)}</Tag>
            <span className="scn2-cmp-time tab-num">{attempt.time}</span>
          </div>
        </div>
        <CloseButton onClick={onClose} className="ws-dialog-x" />
      </div>

      <div className="ws-dialog-body scn2-cmp-body">
        <p className="scn2-cmp-note">这里只保留该次尝试的复盘摘要，不包含可恢复的历史正文快照。重写始终以当前稿为输入，只把这份复盘意见加进作者指令。</p>
        {cmp.verdict
          ? <blockquote className="scn2-cmp-verdict">{cmp.verdict}</blockquote>
          : <p className="scn2-cmp-empty">这一次尝试没有留下复盘意见。</p>}
      </div>

      <div className="ws-dialog-foot">
        <button type="button" className="btn btn-ghost" onClick={onClose}>关闭</button>
        <button type="button" className="btn btn-accent" data-testid="scene-attempt-rewrite" onClick={onRewrite}
          disabled={!onRewrite || !cmp.verdict}
          title={cmp.verdict ? "把这份复盘意见写进作者指令，以当前稿为输入重写" : "这一次尝试没有复盘意见可参考"}>
          <I.Refresh size={13} /> 参考该版复盘意见重写
        </button>
      </div>
    </WsDialog>
  );
}

export { Evidence, AttemptCompare, SceneStyleWindowsPanel };
