import React from "react";
import { I } from "./icons.jsx";
import { SceneDesignCard } from "./ws-scene-design.jsx";
import { EmptyState, Notice, Spinner, Tabs, Tag } from "./ws-ui.jsx";
import { scnCandidates, scnResumeAfterSelection, scnSelectCandidate } from "./ws-scene-api.js";
import {
  RUN_STAGES, runJobStepLabel, scnFindingIsPlainLanguage, scnParaText, scnRunStageIndex,
  scnStyleNoticeSeverity, scnStyleNoticeView, stateLabelOf, stateToneOf,
} from "./ws-scene-derive.js";

const { useEffect, useRef, useState } = React;

/* ==========================================================
   AI 起草台 — 台面（中间一栏）
   场景头 · 七步管线 · 风格提示条，下面按这一场的状态换内容：
   待起草 → 预检（设计卡）；运行中 → 起草说明；待复核 → 正文或候选终选；已归档 → 定稿。
   ========================================================== */

/* ============================ Head ============================ */

function SceneHead({ scene, state, jobQueued, evidence }) {
  const words = scene.words || (scene.verdict && scene.verdict.words) || 0;
  let sub = "";
  if (state === "running") sub = `第 ${scene.attempt || 1} 次尝试`;
  else if (state === "ready") sub = `第 ${scene.attempt || 1} 次尝试 · ${words} 字`;
  else if (state === "archived") sub = scene.justArchived ? "刚刚写入写作台正文" : (scene.archivedAt ? `写入正文于 ${scene.archivedAt}` : "已写入写作台正文");
  return (
    <header className="scn2-head">
      <div className="scn2-head-l">
        <div className="scn2-head-meta">
          <span className="scn2-head-num tab-num">{scene.n}</span>
          <span>{scene.kind}</span>
          <Tag tone={stateToneOf(state, jobQueued)} className="scn2-state-tag">
            {state === "running" && <span className="scn2-chip-pulse" aria-hidden="true" />}
            {stateLabelOf(state, jobQueued)}
          </Tag>
        </div>
        <h1 className="scn2-head-title text-serif" title={scene.title}>{scene.title}</h1>
        {sub && <div className="scn2-head-sub">{sub}</div>}
      </div>
      {evidence && (
        <div className="scn2-head-r">
          <button type="button" ref={evidence.toggleRef || undefined} className={`btn btn-ghost btn-sm scn2-evi-toggle${evidence.open ? " is-on" : ""}`}
            aria-expanded={evidence.open} aria-controls="scn2-evi" onClick={evidence.onToggle}>
            <I.List size={13} /> 证据
          </button>
        </div>
      )}
    </header>
  );
}

/* ============================ Pipeline ============================ */

/* 七步进度只由后端说了算：运行中看 job.current_step，其余看运行记录里的 scene_status（pipeState）。
   认不出来就不打勾——过去它整场都停在「起草」，一出结果就把「质检 / 二改」全勾上，哪怕根本没改。 */
const PIPE_STATUS_SR = { done: "已完成", active: "进行中", wait: "等待中", stop: "停在这一步" };

function Pipeline({ scene, state, job }) {
  const jobIdx = job ? scnRunStageIndex(job.current_step) : -1;
  const pipeIdx = scnRunStageIndex(scene.pipeState);
  const reached = Math.max(jobIdx, pipeIdx);
  const gateState = scene.gate && scene.gate.authorState;
  const stopped = !!(job && ["blocked", "failed", "cancelled"].includes(job.status));
  const statusAt = (i) => {
    if (state === "archived") return "done";
    if (state === "running") {
      const current = jobIdx >= 0 ? jobIdx : Math.max(0, pipeIdx);
      return i < current ? "done" : (i === current ? "active" : "todo");
    }
    if (state === "ready") {
      if (gateState === "awaiting_author_choice") {
        const pause = RUN_STAGES.findIndex(stage => stage.id === "style");
        return i < pause ? "done" : (i === pause ? "wait" : "todo");
      }
      const current = Math.max(reached, 1);   // 有稿至少过了首稿
      if (gateState === "hard_blocked") return i < current ? "done" : (i === current ? "stop" : "todo");
      return i <= current ? "done" : "todo";
    }
    if (job && job.status === "queued") return i === 0 ? "wait" : "todo";
    if (stopped && reached >= 0) return i < reached ? "done" : (i === reached ? "stop" : "todo");
    return "todo";
  };
  return (
    <ol className="scn2-pipe" aria-label="起草管线">
      {RUN_STAGES.map((stage, i) => {
        const st = statusAt(i);
        return (
          <li key={stage.id} className={`scn2-pstep s-${st}`} aria-current={st === "active" ? "step" : undefined}>
            <span className="scn2-pmark" aria-hidden="true">
              {st === "done" && <I.Check size={11} />}
              {st === "active" && <Spinner size={11} />}
              {st === "wait" && <I.Clock size={11} />}
              {st === "stop" && <I.AlertTriangle size={11} />}
              {st === "todo" && <span className="scn2-pidx tab-num">{i + 1}</span>}
            </span>
            <span className="scn2-pname">{stage.name}</span>
            {PIPE_STATUS_SR[st] && <span className="ws-sr-only">（{PIPE_STATUS_SR[st]}）</span>}
          </li>
        );
      })}
    </ol>
  );
}

/* ============================ 风格提示条 ============================ */

/* 每条一行：标题 + 一句白话（按 code / reason / 稿子的阶段说，见 scnStyleNoticeView），按严重度着色；blocking 另带
   is-blocking。词表外的 code 原样回显，后面接「: 后端原话」——新提示绝不吞掉。 */
function SceneStyleNoticeStrip({ notices }) {
  const items = Array.isArray(notices) ? notices.filter((n) => n && n.code) : [];
  if (!items.length) return null;
  return (
    <ul className="scn2-style-notices" data-testid="scene-style-notices" aria-label="风格提示">
      {items.map((n, i) => {
        const severity = scnStyleNoticeSeverity(n.severity);
        const view = scnStyleNoticeView(n);
        return (
          <li
            key={`${n.code}-${i}`}
            className={`scn2-style-notice sev-${severity}${n.blocking || String(n.severity || "").toLowerCase() === "blocking" ? " is-blocking" : ""}`}
            data-code={n.code}
            data-severity={severity}
          >
            {severity === "info" ? <I.Info size={13} /> : <I.AlertTriangle size={13} />}
            <span className="scn2-style-notice-body">
              <strong className="scn2-style-notice-label">{view.label}</strong>
              {view.detail && <span className="scn2-style-notice-msg">{view.known ? ` — ${view.detail}` : `: ${view.detail}`}</span>}
              {n.hitCount != null && <span className="scn2-style-notice-msg">（共 {n.hitCount} 处）</span>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/* ============================ Draft ============================ */

/* 正文只按段落排。旧运行记录里的风险划线（本地质检留下的 parts 切片）一律按纯文本拼回。 */
function Draft({ scene }) {
  return (
    <article className="scn2-draft text-serif">
      {(scene.draft || []).map((p, i) => (
        <p key={p.id || i} className="scn2-para">{scnParaText(p)}</p>
      ))}
    </article>
  );
}

/* ============================ Preflight（待起草） ============================ */

/* 预检 = 起草前对着这一场的设计卡看一眼。一行只列能从卡上读出来的事实；缺拍子、缺 POV 才用警示色。
   设计卡本身占满中间——AI 读的就是它。「在构思里改」只留裁决条上那一个。 */
function Preflight({ model, sync }) {
  if (!model) {
    return (
      <div className="scn2-pre scn2-scroll">
        <EmptyState compact icon="Compass" title="目录里找不到这一场的设计卡">刷新目录后再试；手加的场去章节编排补一张场景卡。</EmptyState>
      </div>
    );
  }
  const fact = (k) => (model.facts.find(f => f.k === k) || {}).v;
  const beatsTotal = model.beats.length || 3;
  const beatsOk = model.beatsFilled >= beatsTotal;
  const checks = [
    { key: "beats", tone: beatsOk ? "ok" : "warn", text: beatsOk ? "三拍齐了" : `三拍缺 ${beatsTotal - model.beatsFilled} 拍`, title: beatsOk ? model.beats.map(b => b.label).join(" / ") : "缺的拍子 AI 只能自己猜" },
    { key: "pov", tone: fact("视角") ? "ok" : "warn", text: fact("视角") ? "视角已定" : "还没定视角" },
    { key: "crucible", tone: model.crucible ? "ok" : "neutral", text: model.crucible ? "坩埚已写" : "坩埚未写", title: "为什么此刻非面对不可（可选）" },
    { key: "place", tone: fact("地点") ? "ok" : "neutral", text: fact("地点") ? "地点已写" : "地点未写", title: "可选" },
  ];
  return (
    <div className="scn2-pre scn2-scroll">
      <div className="scn2-pre-card">
        <ul className="scn2-pre-checks" aria-label="起草前的检查">
          {checks.map(c => (
            <li key={c.key}>
              <Tag tone={c.tone} title={c.title}>
                {c.tone === "ok" ? <I.Check size={11} /> : (c.tone === "warn" ? <I.AlertTriangle size={11} /> : <I.Circle size={10} />)}
                {c.text}
              </Tag>
            </li>
          ))}
        </ul>
        <SceneDesignCard model={model} sync={sync} />
      </div>
    </div>
  );
}

/* ============================ Running ============================ */

/* 起草进行中：没有假百分比、没有按计时器冒出来的日志。当前在哪一步看上面的管线条；
   这里说清楚接下来会发生什么，并把 AI 正在读的那张设计卡摆出来。 */
function RunningStage({ model, job, draftMode }) {
  const step = job && job.current_step ? runJobStepLabel(job.current_step, job.draft_mode || draftMode) : "";
  return (
    <div className="scn2-run scn2-pre scn2-scroll">
      <div className="scn2-pre-card">
        <Notice tone="info" role="note" icon={false}
          title={<span className="scn2-run-title"><Spinner size={13} /> AI 正在起草这一场{step ? `：${step}` : ""}</span>}>
          整稿写完、过完后端质检后回到这里等你裁决。离开这一页不影响后台运行，回来时从后端恢复。
        </Notice>
        {model && <SceneDesignCard model={model} />}
      </div>
    </div>
  );
}

/* ============================ 候选终选（Wave 3 §5.5） ============================ */

/* 盲选：候选稿顺序已随机、不显示机器分数。一次看一稿，页签上只给字数；读完再选。
   onDone(resumed) 由页面取回续跑后的这一场。
   取不到候选（请求失败、后端给了空表）时台面上不能只剩一句话：裁决条在等终选时没有任何按钮，
   所以这里给出「重新取候选」和「重新起草」（onRework，另起一轮、放弃这次终选）两个出口。 */
const CANDIDATE_FETCH_FAILED = "候选稿没取回来";

function CandidatePicker({ sid, onDone, onRework = null }) {
  const [st, setSt] = useState({ loading: true, loadError: null, error: null, candidates: [], picking: null, resuming: false });
  const [styleMatchReason, setStyleMatchReason] = useState(false);
  const [tab, setTab] = useState(0);
  const [reloadTick, setReloadTick] = useState(0);
  const openedAtRef = useRef(Date.now());
  useEffect(() => {
    let on = true;
    openedAtRef.current = Date.now();
    setStyleMatchReason(false);
    setTab(0);
    setSt({ loading: true, loadError: null, error: null, candidates: [], picking: null, resuming: false });
    (async () => {
      try {
        const data = await scnCandidates(sid);
        if (!on) return;
        setSt(s => ({ ...s, loading: false, candidates: (data && data.candidates) || [] }));
      } catch (e) {
        if (on) setSt(s => ({ ...s, loading: false, loadError: (e && e.message) || CANDIDATE_FETCH_FAILED }));
      }
    })();
    return () => { on = false; };
  }, [sid, reloadTick]);

  const choose = async (rowId, tie) => {
    setSt(s => ({ ...s, picking: rowId, error: null }));
    try {
      const selection = {
        no_clear_difference: !!tie,
        duration_ms: Math.max(0, Date.now() - openedAtRef.current),
        preference_tags: styleMatchReason && !tie ? ["style_match"] : [],
      };
      await scnSelectCandidate(sid, rowId, selection);
      setSt(s => ({ ...s, resuming: true }));
      const resumed = await scnResumeAfterSelection(sid);
      if (onDone) await onDone(resumed);
    } catch (e) {
      setSt(s => ({ ...s, picking: null, resuming: false, error: (e && e.message) || "终选失败，请重试" }));
    }
  };

  const letter = (i) => String.fromCharCode(65 + i);
  const candidates = st.candidates;
  const index = Math.min(tab, Math.max(0, candidates.length - 1));
  const current = candidates[index];
  const wordsOf = (c) => String((c && c.content) || "").replace(/\s/g, "").length;
  const busy = !!st.picking || st.resuming;
  return (
    <div className="scn2-review scn2-pick scn2-scroll">
      {(st.loading || candidates.length > 0) && (
        <Notice tone="info" className="scn2-pick-note" title="关键场景 · 匿名候选终选">
          顺序已打乱，不显示机器分数。每一稿都读完再选；选中后管线自动续跑（批判修订、质检、归档）。
        </Notice>
      )}
      {st.error && <Notice tone="danger" className="scn2-pick-note">{st.error}</Notice>}
      {st.loading && <p className="scn2-draft-foot"><Spinner size={12} /> 正在取候选稿…</p>}
      {!st.loading && !candidates.length && (
        <Notice
          tone={st.loadError ? "danger" : "warn"}
          className="scn2-pick-note"
          testId="scene-candidate-empty"
          title={st.loadError ? CANDIDATE_FETCH_FAILED : "后端没有给出可选的候选稿"}
          actions={(
            <>
              <button type="button" className={`btn btn-sm ${st.loadError ? "btn-accent" : "btn-ghost"}`}
                data-testid="scene-candidate-reload" onClick={() => setReloadTick(n => n + 1)}>
                <I.Refresh size={13} /> 重新取候选
              </button>
              {onRework && (
                <button type="button" className={`btn btn-sm ${st.loadError ? "btn-ghost" : "btn-accent"}`}
                  data-testid="scene-candidate-rework" onClick={() => onRework()}
                  title="另起一轮起草，不再等这次终选">
                  重新起草这一场
                </button>
              )}
            </>
          )}
        >
          {st.loadError && st.loadError !== CANDIDATE_FETCH_FAILED && scnFindingIsPlainLanguage(st.loadError)
            ? `${st.loadError.replace(/[。.]$/, "")}。` : ""}
          可以再取一次；仍然没有的话，重新起草这一场（另起一轮，这次终选作废）。
        </Notice>
      )}
      {candidates.length > 0 && (
        <>
          <Tabs
            className="scn2-pick-tabs" label="候选稿" idPrefix="scn2-pick" value={String(index)}
            onChange={(id) => setTab(Number(id))}
            tabs={candidates.map((c, i) => ({ id: String(i), label: `候选 ${letter(i)}`, count: `${wordsOf(c)} 字` }))}
          />
          <article className="scn2-draft text-serif scn2-pick-text" role="tabpanel" id={`scn2-pick-panel-${index}`}
            aria-labelledby={`scn2-pick-tab-${index}`}>
            {String(current.content || "").split(/\n+/).filter(Boolean).map((para, i) => <p key={i} className="scn2-para">{para}</p>)}
          </article>
          <div className="scn2-pick-foot">
            <label className="scn2-pick-reason">
              <input
                type="checkbox"
                data-testid="scene-candidate-style-reason"
                checked={styleMatchReason}
                disabled={busy}
                onChange={e => setStyleMatchReason(e.target.checked)}
              />
              我主要按「更贴近参考风格」来选
            </label>
            {candidates.length > 1 && (
              <button className="btn btn-quiet btn-sm" data-testid="scene-candidate-tie" disabled={busy}
                title="记录「各稿无明显差异」并采用候选 A（终选耗时与平局照实入档）"
                onClick={() => choose(candidates[0].row_id, true)}>
                各稿无明显差异，用候选 A
              </button>
            )}
            <button
              className="btn btn-accent btn-sm"
              data-testid="scene-candidate-select"
              data-candidate-row-id={current.row_id}
              disabled={busy}
              onClick={() => choose(current.row_id, false)}
            >
              <I.Check size={13} /> {st.picking === current.row_id ? (st.resuming ? "续跑中…" : "提交中…") : `选候选 ${letter(index)}`}
            </button>
          </div>
        </>
      )}
      {candidates.length > 0 && <p className="scn2-draft-foot">终选只写一次：提交后要改选，需要在后端显式重开（留审计）。</p>}
    </div>
  );
}

/* ============================ Review / Archived ============================ */

function ReviewStage({ scene }) {
  return (
    <div className="scn2-review scn2-scroll">
      <Draft scene={scene} />
    </div>
  );
}

function ArchivedStage({ scene }) {
  return (
    <div className="scn2-review scn2-scroll">
      {scene.justArchived && (
        <Notice tone="ok" icon={I.Check} role="note">
          已写入 <strong>{scene.n}</strong> 的正文文档（{(scene.verdict && scene.verdict.words) || scene.words || "—"} 字），场景卡已置「完成」，字数已回写目录。
        </Notice>
      )}
      <Draft scene={scene} />
    </div>
  );
}

export { SceneHead, Pipeline, SceneStyleNoticeStrip, Preflight, RunningStage, CandidatePicker, ReviewStage, ArchivedStage };
