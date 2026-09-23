import React from "react";
import { I } from "./icons.jsx";
import { Notice, Spinner } from "./ws-ui.jsx";
import { wrPickedText, wrSentences } from "./writer-candidates.js";
import { wrAiError, wrContinueChips } from "./ws-writer-ai.js";
import { wrContinueMulti } from "./ws-writer-requests.js";

/* ==========================================================
   AI 续写的共用部件（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   托盘（WrTray）与「AI 放在抽屉里」时的抽屉面板用同一份状态（useWrContinuation）、
   同一个候选列表（WrCandidateList）和同一张候选卡；失败提示（WrAiErrorBlock）
   选区改写的弹层也用。ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useRef, useState } = React;

/* AI 面里的失败提示：一句作者能照做的话 + 动作（去系统设置 / 重试；说不清原因时两个都给）。
   抄袭门拦下（kind copy）说的是「哪一版的第几字与参考书原文相同，已丢掉」，不给参考原文。 */
export function WrAiErrorBlock({ error, onRetry, onOpenSettings }) {
  if (!error) return null;
  const info = wrAiError(error);
  const configOnly = info.kind === "config";
  const retry = !configOnly && onRetry
    ? <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>{info.actionLabel}</button> : null;
  const settings = info.offersSettings && onOpenSettings
    ? <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenSettings}>去系统设置</button> : null;
  return (
    <Notice tone={configOnly ? "warn" : "danger"} className="wr-ai-error" testId={`wr-ai-error-${info.kind}`}
      title={info.kind === "copy" ? "照搬了参考书，已丢掉" : undefined}
      actions={retry || settings ? <>{retry}{settings}</> : null}>
      {info.message}
    </Notice>
  );
}

/* ---- 续写状态：托盘与抽屉各持一份，换场即清空 ---- */
export const WR_CONTINUE_DEFAULT = "续写下一段，自然承接当前正文";

export function useWrContinuation(sceneId) {
  const [prompt, setPrompt] = useState(WR_CONTINUE_DEFAULT);
  const [phase, setPhase] = useState("ready"); // ready | loading | result | error
  const [cands, setCands] = useState([]);
  const [error, setError] = useState(null);
  const [picks, setPicks] = useState({});
  const [copyBlocked, setCopyBlocked] = useState(0);
  const reqRef = useRef(0);
  const run = (instruction) => {
    const id = ++reqRef.current;
    const text = String(instruction == null ? prompt : instruction).trim() || WR_CONTINUE_DEFAULT;
    setPhase("loading"); setPicks({}); setError(null); setCopyBlocked(0);
    wrContinueMulti(text, sceneId)
      .then((list) => { if (reqRef.current === id) { setCands(list); setCopyBlocked(Number(list && list.copyBlocked) || 0); setPhase("result"); } })
      .catch((e) => { if (reqRef.current === id) { setCands([]); setError(e); setPhase("error"); } });
  };
  /* 换了一场，上一场的候选就不该还挂着（采纳会插进这一场的正文）；晚到的旧结果也不再落下来 */
  useEffect(() => {
    reqRef.current += 1;
    setPhase("ready"); setCands([]); setError(null); setPicks({}); setCopyBlocked(0);
  }, [sceneId]);
  const toggle = (id, index) => setPicks((prev) => {
    const cur = prev[id] || [];
    return { ...prev, [id]: cur.includes(index) ? cur.filter((x) => x !== index) : [...cur, index] };
  });
  return { prompt, setPrompt, phase, cands, error, picks, copyBlocked, run, toggle };
}

/* 候选按句拆开，点句子只挑那几句 */
function WrCandText({ html, picked, onToggle }) {
  const sentences = wrSentences(html);
  return (
    <p className="wr-cand-text">
      {sentences.map((sentence, index) => (
        <span key={index}
          className={`wr-sen ${picked.includes(index) ? "is-pick" : ""}`}
          onClick={(e) => { e.stopPropagation(); onToggle(index); }}
          dangerouslySetInnerHTML={{ __html: sentence }} />
      ))}
    </p>
  );
}

function WrCandCard({ cand, index, picked, selected, onToggle, onAdopt, onAdoptText, onMerge, onSelect, style }) {
  const sentences = wrSentences(cand.html);
  return (
    <article className={`wr-cand ${selected ? "is-sel" : ""}`} style={style}
      onMouseEnter={onSelect} onClick={onSelect}>
      <div className="wr-cand-head">
        <span className="wr-cand-key" aria-hidden="true">{index + 1}</span>
        <span className={`pill pill-${cand.tone} text-xs`}><span className="pill-dot" />{cand.approach}</span>
        {picked.length > 0 && <span className="wr-cand-pickn">已选 {picked.length} 句</span>}
      </div>
      <WrCandText html={cand.html} picked={picked} onToggle={onToggle} />
      {cand.note && <p className="wr-cand-note">{cand.note}</p>}
      <div className="wr-cand-act">
        <button type="button" className="btn btn-quiet btn-sm" onClick={(e) => { e.stopPropagation(); if (onMerge) onMerge(cand); }}
          title="插到正文末尾，由你改成自己的话；虚线框只标到离开这一场为止">作为草稿插入</button>
        {picked.length > 0
          ? <button type="button" className="btn btn-accent btn-sm" onClick={(e) => { e.stopPropagation(); if (onAdoptText) onAdoptText(wrPickedText(sentences, picked)); }}>采纳选中 {picked.length} 句</button>
          : <button type="button" className="btn btn-accent btn-sm" onClick={(e) => { e.stopPropagation(); onAdopt(cand); }}>采纳整段</button>}
      </div>
    </article>
  );
}

/* 加载中是三张骨架卡，有结果时是三张候选卡；其余阶段什么都不画（提示 / 失败由各自的外壳放）。
   stacked：抽屉里竖排（骨架矮一点）；selected / onSelect：托盘的键盘选择。 */
export function WrCandidateList({ state, stacked = false, selected = -1, onSelect, onAdopt, onAdoptText, onMerge }) {
  if (state.phase === "loading") {
    return [0, 1, 2].map((i) => (
      <div key={i} className={`wr-cand wr-skel ${stacked ? "wr-cand-stack" : ""}`}>
        <div className="sk" /><div className="sk" />{!stacked && <div className="sk" />}<div className="sk short" />
      </div>
    ));
  }
  if (state.phase !== "result") return null;
  const cards = state.cands.map((cand, i) => (
    <WrCandCard key={cand.id} cand={cand} index={i} picked={state.picks[cand.id] || []}
      selected={selected === i} onSelect={onSelect ? () => onSelect(i) : undefined}
      onToggle={(si) => state.toggle(cand.id, si)}
      onAdopt={onAdopt} onAdoptText={onAdoptText} onMerge={onMerge}
      style={{ animationDelay: i * 70 + "ms" }} />
  ));
  if (!state.copyBlocked) return cards;
  /* 服务端丢掉了照搬参考书的那几版：说一句，免得作者纳闷为什么不是三条 */
  return [
    <p key="copy-note" className="wr-cand-copy-note" role="note" data-testid="wr-cand-copy-note">
      另有 {state.copyBlocked} 版与参考书原文连续相同（或用了它的专名），已经丢掉。
    </p>,
    ...cards,
  ];
}

/* 续写提示的快捷词：只设置提示，不直接生成 */
export function WrContinueChips({ design, onPick }) {
  const chips = wrContinueChips(design);
  return (
    <div className="wr-chips" aria-label="续写提示快捷词">
      {chips.map((chip) => (
        <button type="button" key={chip.label} className="pill wr-chip" title={chip.prompt} onClick={() => onPick(chip.prompt)}>{chip.label}</button>
      ))}
    </div>
  );
}

/* 「AI 放在抽屉里」时上下文栏的 AI 页签 */
export function WrContinuePanel({ sceneId, design, onAdopt, onMerge, onAdoptText, onOpenSettings }) {
  const c = useWrContinuation(sceneId);
  const inputRef = useRef(null);
  const loading = c.phase === "loading";
  return (
    <div className="wr-ai-panel">
      <label className="wr-ai-label" htmlFor="wr-ai-prompt">续写提示</label>
      <textarea id="wr-ai-prompt" ref={inputRef} className="wr-prompt-in" rows={3} value={c.prompt}
        onChange={(e) => c.setPrompt(e.target.value)} />
      <WrContinueChips design={design} onPick={(text) => { c.setPrompt(text); if (inputRef.current) inputRef.current.focus(); }} />
      <button type="button" className="btn btn-accent btn-sm wr-ai-run" disabled={loading} onClick={() => c.run()}>
        {loading ? <Spinner size={13} /> : <I.Wand size={14} />} {loading ? "正在生成…" : c.phase === "result" ? "重新生成三条" : "生成三条续写"}
      </button>
      <p className="wr-ai-note">续写只在正文末尾追加下一段，不改已写的文字；要改已写的句子，选中它，用弹出的工具条。</p>
      {c.phase === "error" && <WrAiErrorBlock error={c.error} onRetry={() => c.run()} onOpenSettings={onOpenSettings} />}
      {(loading || c.phase === "result") && (
        <div className="wr-block mt-4">
          <div className="wr-block-h">三个方向 · 点句子可以只挑几句</div>
          <WrCandidateList state={c} stacked onAdopt={onAdopt} onAdoptText={onAdoptText} onMerge={onMerge} />
        </div>
      )}
    </div>
  );
}
