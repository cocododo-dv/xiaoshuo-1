import React from "react";
import { I } from "./icons.jsx";
import { ARR_ACTS, ARR_ARCHIVED, ARR_CH_STATE, ARR_SCENE_STATE, ARR_THREAD_ROLE } from "./ws-author-data.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { ArrThreadLoom, ArrThreadMini, arrDeriveThreads } from "./ws-author-loom.jsx";
import { ArrPacingLens } from "./ws-author-pacing.jsx";
import { ArrDoctor } from "./ws-author-doctor.jsx";
import { wsKey, WsWorks } from "./ws-works.jsx";
import { ArrChapterRunAction } from "./ws-chapter-run.jsx";
import { ArrBlueprintCard, ArrPlanPanel, ArrAiHealthBlock } from "./ws-author-plan.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { navigateWithViewIntent } from "./ws-view-intents.js";

/* global React, I, ARR_ACTS, ARR_CH_STATE, ARR_SCENE_STATE, ARR_THREAD_ROLE, ARR_ARCHIVED, ArrThreadLoom, ArrPacingLens, ArrThreadMini, arrDeriveThreads, ArrDoctor */
const { useState: useStA, useRef: useRefA, useEffect: useEfA, useMemo: useMemoA } = React;

/* 卷名/简介是可选装饰；真实作品只显示
   通用卷序（卷一/卷二/卷三），不泄漏演示卷名。章节本身走 WsCatalog（已按作品门控）。 */

/* ==========================================================
   章节编排 — Chapter Arrangement
   两种模式：
   · 全书编排（overview）—— 故事弧线曲线 + 按卷分组的章节看板，可拖动重排
   · 章节详情（detail）—— 序列栏 · 编辑器（脉络 / 戏剧卡 / 场景看板）· 章节体检
   ========================================================== */

/* ---- tiny persistence (per-work namespaced) ---- */
const arrK = (k) => (wsKey ? wsKey(k) : k);
const arrLsGet = (k, d) => { try { const v = localStorage.getItem(arrK(k)); return v == null ? d : JSON.parse(v); } catch (_) { return d; } };
const arrLsSet = (k, v) => { try { localStorage.setItem(arrK(k), JSON.stringify(v)); } catch (_) {} };

/* ensure every scene carries a stable id (for inline-edit + reorder identity) */
const arrStampIds = (list) => list.map((c) => ({
  ...c,
  scenes: c.scenes.map((s, i) => (s.sid ? s : { ...s, sid: c.id + "-s" + i + "-" + Math.random().toString(36).slice(2, 6) })),
}));

/* ---- drag-to-reorder hook (constrained within a group) ---- */
function useArrOrder(ids) {
  const key = ids.join("|");
  const [order, setOrder] = useStA(ids);
  const [drag, setDrag] = useStA(null);
  useEfA(() => { setOrder(ids); }, [key]);
  const move = (arr, fromId, toId) => {
    const a = [...arr]; const f = a.indexOf(fromId), t = a.indexOf(toId);
    if (f < 0 || t < 0) return arr; a.splice(f, 1); a.splice(t, 0, fromId); return a;
  };
  const handlers = (id, group) => ({
    draggable: true,
    onDragStart: (e) => { setDrag({ id, group }); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", id); } catch (_) {} },
    onDragEnter: () => { if (!drag || drag.id === id || drag.group !== group) return; setOrder((p) => move(p, drag.id, id)); },
    onDragOver: (e) => e.preventDefault(),
    onDrop: (e) => { e.preventDefault(); setDrag(null); },
    onDragEnd: () => setDrag(null),
    "data-dragging": drag && drag.id === id ? "true" : undefined,
  });
  return [order, handlers, drag];
}

/* ---- pills ---- */
function ArrChPill({ s, sm }) {
  const m = ARR_CH_STATE[s] || ARR_CH_STATE.draft;
  return <span className={`pill pill-${m.tone} ${sm ? "text-xs" : ""}`}><span className="pill-dot" />{m.label}</span>;
}
function ArrScenePill({ s }) {
  const m = ARR_SCENE_STATE[s] || ARR_SCENE_STATE.todo;
  return <span className={`pill pill-${m.tone} text-xs`}><span className="pill-dot" />{m.label}</span>;
}

/* ---- scene progress dots ---- */
function ArrMiniScenes({ scenes }) {
  return (
    <span className="arr-dots" title={`${scenes.length} 场`}>
      {scenes.map((s, i) => (
        <span key={i} className="arr-dot" style={{ background: (ARR_SCENE_STATE[s.state] || ARR_SCENE_STATE.todo).dot }} />
      ))}
    </span>
  );
}

/* ---- word budget bar ---- */
function ArrBudgetBar({ cur, target, compact }) {
  const pct = target ? Math.min(100, Math.round((cur / target) * 100)) : 0;
  const over = cur > target * 1.08;
  return (
    <div className={`arr-budget ${compact ? "is-compact" : ""}`}>
      <div className="arr-budget-track">
        <div className={`arr-budget-fill ${over ? "is-over" : ""} ${cur === 0 ? "is-empty" : ""}`} style={{ width: (cur === 0 ? 0 : Math.max(4, pct)) + "%" }} />
      </div>
      {!compact && (
        <div className="arr-budget-num">
          <span className="tab-num">{cur.toLocaleString()}</span>
          <span className="arr-budget-sep">/ {target.toLocaleString()}</span>
        </div>
      )}
    </div>
  );
}

/* ==========================================================
   全书编排 — ArrOverview
   ========================================================== */

function arrSmoothPath(pts) {
  if (pts.length < 2) return "";
  let d = `M ${pts[0].x} ${pts[0].y}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6;
    const c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p2.x} ${p2.y}`;
  }
  return d;
}

function ArrTensionCurve({ chapters, numOf, pickedId, onPick }) {
  const W = 1000, H = 248, padL = 40, padR = 24, padT = 30, padB = 34;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const n = chapters.length;
  const x = (i) => padL + (n <= 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const y = (t) => padT + (1 - t) * plotH;
  const pts = chapters.map((c, i) => ({ x: x(i), y: y(c.tension), c, i }));
  const line = arrSmoothPath(pts);
  // 单章（pts<2）时 arrSmoothPath 返回 ""，若仍拼接 area 会得到以 " L" 开头、缺起始 M 的非法 path，
  // 浏览器报 "<path> attribute d: Expected moveto"。line 为空时 area 也置空（单点只渲染圆点即可）。
  const area = line ? line + ` L ${pts[n - 1].x} ${padT + plotH} L ${pts[0].x} ${padT + plotH} Z` : "";

  // act bands
  const bands = ARR_ACTS.map((a) => {
    const idxs = chapters.map((c, i) => (c.act === a.id ? i : -1)).filter((i) => i >= 0);
    if (!idxs.length) return null;
    const first = Math.min(...idxs), last = Math.max(...idxs);
    const x0 = first === 0 ? 0 : (x(first) + x(first - 1)) / 2;
    const x1 = last === n - 1 ? W : (x(last) + x(last + 1)) / 2;
    return { a, x0, x1, mid: (x0 + x1) / 2 };
  }).filter(Boolean);

  const stateColor = (s) => ({ approved: "var(--sage)", review: "var(--gold)", draft: "var(--ink-4)", writing: "var(--crimson)", planned: "var(--line-3)" }[s] || "var(--ink-4)");

  return (
    <svg className="arr-curve" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="全书张力弧线">
      <defs>
        <linearGradient id="arrCurveFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--crimson)" stopOpacity="0.16" />
          <stop offset="100%" stopColor="var(--crimson)" stopOpacity="0" />
        </linearGradient>
      </defs>

      {/* act bands */}
      {bands.map((b, i) => (
        <g key={b.a.id}>
          {i > 0 && <line x1={b.x0} y1={padT - 8} x2={b.x0} y2={padT + plotH + 6} className="arr-curve-actsep" />}
          <text x={b.mid} y={18} className={`arr-curve-actlabel tone-${b.a.tone}`} textAnchor="middle">{b.a.n}</text>
        </g>
      ))}

      {/* gridlines */}
      {[0.25, 0.5, 0.75].map((t) => (
        <line key={t} x1={padL} y1={y(t)} x2={W - padR} y2={y(t)} className="arr-curve-grid" />
      ))}
      <text x={padL - 8} y={y(0.85) + 4} className="arr-curve-axis" textAnchor="end">高</text>
      <text x={padL - 8} y={y(0.12) + 4} className="arr-curve-axis" textAnchor="end">低</text>

      {/* current vertical guide */}
      {pts.filter((p) => p.c.id === pickedId).map((p) => (
        <line key="guide" x1={p.x} y1={padT - 4} x2={p.x} y2={padT + plotH + 4} className="arr-curve-guide" />
      ))}

      <path d={area} fill="url(#arrCurveFill)" />
      <path d={line} className="arr-curve-line" fill="none" />

      {/* dots */}
      {pts.map((p) => {
        const active = p.c.id === pickedId;
        return (
          <g key={p.c.id} className="arr-curve-node" onClick={() => onPick(p.c.id)} style={{ cursor: "pointer" }}>
            {p.c.current && <circle cx={p.x} cy={p.y} r="10" className="arr-curve-pulse" />}
            <circle cx={p.x} cy={p.y} r={active ? 7 : 5} fill={stateColor(p.c.state)} stroke="var(--paper-0)" strokeWidth="2" />
            <text x={p.x} y={padT + plotH + 22} className={`arr-curve-xlabel ${active ? "is-active" : ""}`} textAnchor="middle">{numOf[p.c.id]}</text>
            <title>{`第 ${numOf[p.c.id]} 章 · ${p.c.title} — 张力 ${Math.round(p.c.tension * 100)}`}</title>
          </g>
        );
      })}
    </svg>
  );
}

function ArrChapterCard({ c, num, picked, onOpen, dnd, selectMode, selected, onToggleSelect }) {
  const done = c.scenes.filter((s) => s.state === "done").length;
  const locked = c.state === "approved";
  /* 阶段 X：雪花整理出来的章没有「章承诺」和章级 POV——卡上不留空，用构思里的章摘要和各场的 POV 顶上 */
  const blurb = c.promise || c.summary || "";
  const povLine = c.pov || [...new Set(c.scenes.map((s) => s.povName).filter(Boolean))].slice(0, 3).join(" · ");
  return (
    <div className={`arr-card s-${c.state} ${picked ? "is-picked" : ""} ${selected ? "is-selected" : ""} ${selectMode && locked ? "is-unselectable" : ""}`}
      {...(selectMode ? {} : dnd)}
      onClick={() => { if (!selectMode) { onOpen(c.id); return; } if (!locked) onToggleSelect(c.id); }}>
      {selectMode ? (
        <label className="arr-card-check" onClick={(e) => e.stopPropagation()}
          title={locked ? "已批准终稿不可删除——请先到成稿中心重新打开" : "选中本章"}>
          <input type="checkbox" checked={!!selected} disabled={locked}
            aria-label={`选择第 ${num} 章 · ${c.title}`}
            onChange={() => onToggleSelect(c.id)} />
        </label>
      ) : (
        <span className="arr-card-grip" title="拖动重排（可跨卷）"><I.GripVertical size={15} /></span>
      )}
      <div className="arr-card-top">
        <span className="arr-card-num">{num}</span>
        <ArrChPill s={c.state} sm />
        {c.spine && <span className="arr-card-spine" title="这一章收在这个灾难上（来自构思的分章）">{c.spine}</span>}
      </div>
      <div className="arr-card-title text-serif">{c.title}</div>
      <div className="arr-card-promise" title={blurb}>{blurb}</div>
      <div className="arr-card-foot">
        <span className="arr-card-scenes"><ArrMiniScenes scenes={c.scenes} /><span className="arr-card-scenes-num">{done}/{c.scenes.length}</span></span>
        <span className="arr-card-pov"><I.Eye size={12} />{povLine}</span>
      </div>
      <ArrBudgetBar cur={c.words.cur} target={c.words.target} />
    </div>
  );
}

function ArrOverview({ chapters, numOf, pickedId, onOpen, chDnd, boardDnd, onNew, lens, setLens, batch }) {
  const totalTarget = chapters.reduce((s, c) => s + c.words.target, 0);
  const totalCur = chapters.reduce((s, c) => s + c.words.cur, 0);
  const drafted = chapters.filter((c) => c.state !== "planned").length;
  const approved = chapters.filter((c) => c.state === "approved").length;
  const bt = batch || {};
  const selectMode = !!bt.mode;

  return (
    <div className="arr-ov-scroll">
      {/* lens: story arc / thread loom */}
      <section className="card arr-arc">
        <div className="card-head">
          <div>
            <div className="card-title">{lens === "loom" ? "线索织布机" : lens === "pace" ? "节奏镜头" : "故事弧线"}</div>
            <div className="card-sub">
              {lens === "loom"
                ? "每条线索从引入到收束的全书走向。虚线表示尚未收束，点节点进入该章。"
                : lens === "pace"
                ? "按章字数直方图（虚影为目标）+ POV 着色与泳道。看哪一章注水、哪一章太薄、POV 切换是否健康。"
                : "全书张力随章推进的走势。点圆点可直接进入该章。"}
            </div>
          </div>
          <div className="arr-arc-head-r">
            <div className="seg">
              <button className={`seg-btn ${lens === "arc" ? "is-active" : ""}`} onClick={() => setLens("arc")}>故事弧线</button>
              <button className={`seg-btn ${lens === "loom" ? "is-active" : ""}`} onClick={() => setLens("loom")}>线索织布机</button>
              <button className={`seg-btn ${lens === "pace" ? "is-active" : ""}`} onClick={() => setLens("pace")}>节奏镜头</button>
            </div>
            {lens === "arc" && (
              <div className="arr-arc-legend">
                <span><i className="arr-lg" style={{ background: "var(--sage)" }} />已批准</span>
                <span><i className="arr-lg" style={{ background: "var(--gold)" }} />审阅</span>
                <span><i className="arr-lg" style={{ background: "var(--crimson)" }} />进行</span>
                <span><i className="arr-lg" style={{ background: "var(--line-3)" }} />规划</span>
              </div>
            )}
          </div>
        </div>
        {lens === "loom"
          ? <ArrThreadLoom chapters={chapters} numOf={numOf} onOpen={onOpen} />
          : lens === "pace"
          ? <ArrPacingLens chapters={chapters} numOf={numOf} onOpen={onOpen} />
          : <ArrTensionCurve chapters={chapters} numOf={numOf} pickedId={pickedId} onPick={onOpen} />}
      </section>

      {/* book stats */}
      <div className="arr-ov-stats">
        <ArrMetric k="卷" v={ARR_ACTS.length} sub="三幕结构" />
        <ArrMetric k="章节" v={chapters.length} sub={`${drafted} 起草 · ${approved} 批准`} />
        <ArrMetric k="字数" v={totalCur.toLocaleString()} sub={`目标 ${totalTarget.toLocaleString()}`} />
        {/* 雪花刚整理出来的章还没有字数目标（target 为空）：0 / 0 不是 NaN% */}
        <ArrMetric k="进度" v={(totalTarget > 0 ? Math.round((totalCur / totalTarget) * 100) : 0) + "%"} sub="全书完成度" tone="crimson" />
      </div>

      {/* book doctor */}
      <ArrDoctor chapters={chapters} numOf={numOf} onOpen={onOpen} onLens={setLens} />

      {/* board grouped by act */}
      {ARR_ACTS.map((a) => {
        const items = chapters.filter((c) => c.act === a.id);
        const w = items.reduce((s, c) => s + c.words.cur, 0);
        return (
          <section className="arr-actsec" key={a.id}>
            <header className="arr-actsec-head">
              <span className={`arr-actsec-tag tone-${a.tone}`}>{a.n}</span>
              <h3 className="arr-actsec-name text-serif"></h3>
              <span className="arr-actsec-blurb"></span>
              <span className="arr-actsec-meta tab-num">{items.length} 章 · {w.toLocaleString()} 字</span>
            </header>
            <div className="arr-board" {...boardDnd(a.id)}>
              {/* 多选态不再叠加「当前章」高亮：两者都是 crimson 描边，同屏出现分不清
                  哪张是选中的、哪张只是上次打开过的 */}
              {items.map((c) => (
                <ArrChapterCard key={c.id} c={c} num={numOf[c.id]} picked={!selectMode && c.id === pickedId} onOpen={onOpen} dnd={chDnd(c.id)}
                  selectMode={selectMode} selected={selectMode && bt.has && bt.has(c.id)} onToggleSelect={bt.onToggle} />
              ))}
              {!selectMode && <button className="arr-card-add" onClick={() => onNew(a.id)}><I.Plus size={16} /><span>在{a.n}新建章节</span></button>}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function ArrMetric({ k, v, sub, tone }) {
  return (
    <div className={`arr-metric ${tone ? "tone-" + tone : ""}`}>
      <div className="arr-metric-k">{k}</div>
      <div className="arr-metric-v tab-num">{v}</div>
      <div className="arr-metric-sub">{sub}</div>
    </div>
  );
}

/* ==========================================================
   章节详情 — Detail
   ========================================================== */

const ARR_DRAMA_GROUPS = [
  { key: "promise", label: "承诺", icon: "Star", tone: "crimson", fields: [
    { k: "promise", label: "核心承诺", hint: "读完这一章读者会得到什么", primary: true },
    { k: "problem", label: "章节问题", hint: "本章想问读者一个什么问题" },
  ] },
  { key: "drive", label: "推进", icon: "ArrowRight", tone: "gold", fields: [
    { k: "spine", label: "主线推进", hint: "本章在全书主线上前进了多少" },
    { k: "arc", label: "人物变化", hint: "主要人物的内在或外在变化" },
  ] },
  { key: "close", label: "收束", icon: "Sparkles", tone: "sage", fields: [
    { k: "aftertaste", label: "结尾余味", hint: "读完最后一段的感觉" },
    { k: "ending", label: "结尾效果", hint: "最后一句具体的画面 / 动作" },
  ] },
];

function ArrDramaField({ f, value, ck, onCommit, locked = false }) {
  return (
    <div className={`arr-field ${f.primary ? "is-primary" : ""}`}>
      <header className="arr-field-head">
        <span className="arr-field-label">{f.label}</span>
        <span className="arr-field-hint">{f.hint}</span>
      </header>
      <textarea className="arr-field-text" defaultValue={value} key={ck} placeholder="待填…" disabled={locked}
        aria-label={f.label}
        onBlur={(e) => onCommit && onCommit(e.target.value)} />
    </div>
  );
}

function ArrGmcEdit({ s, onEdit, locked = false }) {
  const bits = s.kind === "反应"
    ? [["反应", "goal"], ["困境", "obstacle"], ["决定", "turn"]]
    : [["目标", "goal"], ["阻碍", "obstacle"], ["出口", "turn"]];
  // POV 角色候选（best-effort，取自资料库人物；冷启动无角色时为空，仍可自由输入新名）
  const povChars = (() => { try { return (window.LIB_ENTRIES || []).filter(e => e.cat === "people").map(e => e.name).filter(Boolean); } catch (e) { return []; } })();
  const povListId = "arr-pov-" + s.sid;
  return (
    <span className="arr-scene-brief">
      {bits.map(([label, key]) => (
        <span key={key} className="arr-gmc">
          <b>{label}</b>
          <input className="arr-gmc-input" defaultValue={s[key] === "—" ? "" : s[key]} key={s.sid + key + (s[key] || "")}
            placeholder="待定" onClick={(e) => e.stopPropagation()} disabled={locked} aria-label={`${s.title} · ${label}`}
            onBlur={(e) => onEdit({ [key]: e.target.value.trim() })}
            onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }} />
        </span>
      ))}
      <span className="arr-gmc arr-gmc-pov">
        <b>POV</b>
        <input className="arr-gmc-input" list={povListId} defaultValue={s.povName || ""} key={s.sid + "pov" + (s.povName || "")}
          placeholder="谁的视角" title="设这一场的 POV 角色（按名字；新角色会自动建档）。起草前置：执行契约需要 POV"
          onClick={(e) => e.stopPropagation()} disabled={locked} aria-label={`${s.title} · POV`}
          onBlur={(e) => onEdit({ povName: e.target.value.trim() })}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }} />
        {povChars.length ? <datalist id={povListId}>{povChars.map((n, i) => <option key={i} value={n} />)}</datalist> : null}
      </span>
    </span>
  );
}

function ArrSceneRow({ s, n, picked, onPick, onCycleKind, onDelete, onEdit, dragHandle, dropZone, locked = false, selectMode = false, selected = false, onToggleSelect }) {
  /* 分流执行：同一张场景卡，自己写去写作台，或交给 AI 起草台排队 */
  const forkWrite = (e) => {
    e.stopPropagation();
    navigateWithViewIntent("writer", "ws:writer-scene", s.sid);
  };
  const forkAI = (e) => {
    e.stopPropagation();
    navigateWithViewIntent("scene", "ws:scene-enqueue", { sid: s.sid });
  };
  return (
    <li className={`arr-scene s-${s.state} ${picked ? "is-active" : ""} ${selected ? "is-selected" : ""} ${selectMode ? "is-selecting" : ""}`} {...dropZone}
      onClick={() => { if (selectMode) { if (!locked) onToggleSelect(s.sid); return; } onPick(); }}>
      {selectMode ? (
        <label className="arr-scene-check" onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={!!selected} disabled={locked} aria-label={`选择场景 ${s.title}`}
            onChange={() => onToggleSelect(s.sid)} />
        </label>
      ) : (
        <span className="arr-scene-grip" title={locked ? "终稿已锁定" : "拖动重排"} {...dragHandle}><I.GripVertical size={14} /></span>
      )}
      <span className="arr-scene-num">{n}</span>
      {/* 多选态：整行只做「选择」——正文编辑、分流执行、单删一律收起，
          否则一个模式里同时摆着四种含义的点击，误操作只是时间问题 */}
      <span className="arr-scene-body" onClick={(e) => { if (!selectMode) e.stopPropagation(); }}>
        <input className="arr-scene-title-input text-serif" defaultValue={s.title} key={s.sid + s.title}
          disabled={locked || selectMode}
          onBlur={(e) => onEdit({ title: e.target.value.trim() || "未命名场景" })}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }} aria-label="场景标题" />
        <ArrGmcEdit s={s} onEdit={onEdit} locked={locked || selectMode} />
      </span>
      {!selectMode && (
        <span className="arr-scene-fork" onClick={(e) => e.stopPropagation()}>
          <button className="arr-fork-btn" disabled={locked} title={locked ? "请先在成稿中心重新打开终稿" : "把这张场景卡送入 AI 起草台排队"} onClick={forkAI}><I.Play size={11} /> 交给 AI</button>
          <button className="arr-fork-btn is-write" disabled={locked} title={locked ? "请先在成稿中心重新打开终稿" : "带着这张卡去写作台写这一场"} onClick={forkWrite}><I.Pen size={11} /> 自己写</button>
        </span>
      )}
      <span className="arr-scene-tags">
        <button className="arr-pill-btn arr-cyc" disabled={locked || selectMode} title={locked ? "终稿已锁定" : "点击切换 主动 / 反应"} onClick={(e) => { e.stopPropagation(); onCycleKind && onCycleKind(); }}>
          <span className={`pill text-xs ${s.kind === "主动" ? "pill-crimson" : "pill-slate"}`}><span className="pill-dot" />{s.kind}</span>
        </button>
        <span className="arr-pill-readonly" title="场景完成状态由正文归档流程推进"><ArrScenePill s={s.state} /></span>
      </span>
      {!selectMode && (
        <button className="btn btn-quiet btn-sm arr-scene-more" disabled={locked} title={locked ? "终稿已锁定" : "移入服务端回收站"} onClick={(e) => { e.stopPropagation(); onDelete && onDelete(); }}><I.Trash size={13} /></button>
      )}
    </li>
  );
}

function ArrHandoffStrip({ prev, ch, next, numOf, onJump }) {
  return (
    <div className="arr-handoff">
      <button className={`arr-ho-cell arr-ho-side ${prev ? "" : "is-empty"}`} disabled={!prev} onClick={() => prev && onJump(prev.id)}>
        <span className="arr-ho-k"><I.ChevronLeft size={12} />承接 {prev ? "CH " + numOf[prev.id] : ""}</span>
        <span className="arr-ho-text">{prev ? prev.exit : "全书开篇 · 无前章"}</span>
      </button>
      <div className="arr-ho-cell arr-ho-mid">
        <span className="arr-ho-k">本章 · 入口 → 出口</span>
        <span className="arr-ho-text arr-ho-entry"><i className="arr-ho-tick">入</i>{ch.entry}</span>
        <span className="arr-ho-text arr-ho-exit"><i className="arr-ho-tick is-out">出</i>{ch.exit}</span>
      </div>
      <button className={`arr-ho-cell arr-ho-side ${next ? "" : "is-empty"}`} disabled={!next} onClick={() => next && onJump(next.id)}>
        <span className="arr-ho-k">交棒 {next ? "CH " + numOf[next.id] : ""}<I.ChevronRight size={12} /></span>
        <span className="arr-ho-text">{next ? next.entry : "全书收束 · 无后章"}</span>
      </button>
    </div>
  );
}

function ArrEditor({ ch, num, prev, next, numOf, sceneTab, setSceneTab, sceneDragHandle, sceneDropZone, onAddScene, onCycleKind, onDeleteScene, onEditScene, onPatchTitle, onPatchDrama, onDeleteChapter, onOpenTrash, pickedScene, setPickedScene, onJump, onBack, snow, chapterRun, sceneBatch }) {
  const tallies = { todo: 0, writing: 0, done: 0 };
  ch.scenes.forEach((s) => { tallies[s.state] = (tallies[s.state] || 0) + 1; });
  const locked = ch.state === "approved";
  const sb = sceneBatch || {};
  const sceneSelectMode = !!sb.mode && !locked;
  const sceneSelected = ch.scenes.filter((s) => sb.has && sb.has(s.sid)).length;

  return (
    <section className="arr-ed">
      <header className="arr-ed-head">
        <div className="arr-ed-head-l">
          <div className="arr-ed-eyebrow">
            <button className="arr-back" onClick={onBack} title="返回全书编排"><I.Layers size={13} />全书编排</button>
            <span className="arr-crumb-sep">/</span>
            <span>CH {num} · 当前编辑</span>
          </div>
          <input className="arr-ed-title text-serif arr-ed-title-input" defaultValue={ch.title} key={ch.id}
            disabled={locked}
            onBlur={(e) => onPatchTitle(e.target.value.trim() || "未命名章节")}
            onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }} aria-label="章节标题" />
        </div>
        <div className="arr-ed-actions">
          <span className="arr-pill-readonly" title="章节状态由运行、审阅与终稿批准流程推进。"><ArrChPill s={ch.state} /></span>
          <button className="btn btn-quiet btn-sm" onClick={() => {
            const card = document.querySelector(".arr-drama");
            if (card && card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "start" });
          }}>大纲快查</button>
          <ArrChapterRunAction chapter={ch} {...chapterRun} />
          <button className="btn btn-quiet btn-sm arr-del-ch" disabled={locked} title={locked ? "终稿已锁定，请先在成稿中心重新打开" : "删除本章"} onClick={onDeleteChapter}><I.Trash size={13} /></button>
          <span className="arr-auto-save" title="章节改动会立即写入目录，并由后端版本收敛。"><I.Check size={12} /> 自动保存</span>
        </div>
      </header>

      <div className="arr-ed-body">
        {locked && (
          <div className="arr-locked-note" role="status">
            <I.Lock size={14} /> 本章是已批准终稿，章节结构与场景卡均为只读。需要修改请先到成稿中心「重新打开」。
          </div>
        )}
        <ArrHandoffStrip prev={prev} ch={ch} next={next} numOf={numOf} onJump={onJump} />

        {/* drama card */}
        <section className="card arr-drama">
          <div className="card-head">
            <div>
              <div className="card-title">戏剧卡</div>
              <div className="card-sub">让章节先有可读的承诺、推进和余味，再交给场景去写。</div>
            </div>
            <button className="btn btn-quiet btn-sm" onClick={snow && snow.onSync} disabled={locked || (snow && snow.busy)}
              title={(!snow || !snow.ready) ? "本作还没从构思物化——点此去构思整理章节结构" : (snow.pending ? `构思有 ${snow.pending} 场改动待回流，点此同步到目录` : "重新从雪花回流场景卡（三拍 / POV / 章 brief）")}>
              <I.Refresh size={13} /> {snow && snow.busy ? "同步中…" : "从雪花同步"}{snow && snow.ready && snow.pending ? ` · ${snow.pending}` : ""}
            </button>
          </div>

          <div className="arr-drama-groups">
            {ARR_DRAMA_GROUPS.map((g) => {
              const Ic = I[g.icon] || I.Dot;
              return (
                <div className="arr-dgroup" key={g.key}>
                  <header className={`arr-dgroup-head tone-${g.tone}`}><Ic size={13} /><span>{g.label}</span><i className="arr-dgroup-rule" /></header>
                  <div className="arr-dgroup-fields">
                    {g.fields.map((f) => <ArrDramaField key={f.k} f={f} value={ch.drama[f.k]} ck={ch.id} locked={locked} onCommit={(v) => onPatchDrama(f.k, v)} />)}
                  </div>
                </div>
              );
            })}
            <div className="arr-dgroup arr-dgroup-guard">
              <header className="arr-dgroup-head tone-slate"><I.ShieldCheck size={13} /><span>护栏</span><i className="arr-dgroup-rule" /></header>
              <div className="arr-guard-grid">
                <div className="arr-guard tone-rose">
                  <div className="arr-guard-label"><I.Ban size={12} /> 禁止包含</div>
                  <textarea className="arr-guard-text" aria-label="禁止包含" disabled={locked} defaultValue={ch.drama.forbidden} key={ch.id + "-fb"} onBlur={(e) => onPatchDrama("forbidden", e.target.value)} />
                </div>
                <div className="arr-guard tone-slate">
                  <div className="arr-guard-label"><I.Quote size={12} /> 备注</div>
                  <textarea className="arr-guard-text" aria-label="章节备注" disabled={locked} defaultValue={ch.drama.notes} key={ch.id + "-nt"} onBlur={(e) => onPatchDrama("notes", e.target.value)} />
                </div>
              </div>
            </div>
          </div>
          {/* 章节蓝图（chapter_story_architecture 一等公民）：作者确认的蓝图注入本章每场起草 */}
          <ArrBlueprintCard ch={ch} locked={locked} />
        </section>

        {/* scene board */}
        <section className="card arr-scenes">
          {/* 与全书编排同一套模式语言：多选态下看板头部只剩批量操作 */}
          <div className="card-head">
            {sceneSelectMode ? (
              <div className="arr-batch is-inline" role="toolbar" aria-label="场景批量操作">
                <span className="arr-batch-n">已选 <strong className="tab-num">{sceneSelected}</strong> / {ch.scenes.length} 场</span>
                <span className="arr-batch-hint">点行选中；删除后进入回收站，可恢复。</span>
                <button className="btn btn-quiet btn-sm" onClick={sb.onSelectAll}>
                  {sceneSelected === ch.scenes.length && ch.scenes.length ? "取消全选" : "全选"}
                </button>
                <button className="btn btn-danger btn-sm" data-testid="author-batch-delete-scenes" disabled={!sceneSelected} onClick={sb.onDelete}>
                  <I.Trash size={13} /> 删除所选{sceneSelected ? ` · ${sceneSelected} 场` : ""}
                </button>
                <button className="btn btn-ghost btn-sm" data-testid="author-scene-select-exit" onClick={sb.onToggleMode}>完成</button>
              </div>
            ) : (
              <React.Fragment>
                <div>
                  <div className="card-title">场景看板</div>
                  <div className="card-sub">排场景顺序、标记结尾场景，把旧版本移入回收。拖动 ⠿ 可重排。</div>
                </div>
                <div className="flex gap-2 items-center">
                  <div className="seg">
                    <button className={`seg-btn ${sceneTab === "active" ? "is-active" : ""}`} onClick={() => setSceneTab("active")}>活跃 {ch.scenes.length}</button>
                    <button className={`seg-btn ${sceneTab === "archived" ? "is-active" : ""}`} onClick={() => setSceneTab("archived")}>回收站</button>
                  </div>
                  {sceneTab === "active" && (
                    <button className="btn btn-quiet btn-sm" data-testid="author-scene-select-mode" disabled={locked || !ch.scenes.length}
                      title={locked ? "终稿已锁定" : "多选场景后可一次删除"} onClick={sb.onToggleMode}>
                      <I.Check size={13} /> 多选
                    </button>
                  )}
                  <button className="btn btn-accent btn-sm" disabled={locked} onClick={onAddScene}><I.Plus size={13} /> 新场景</button>
                </div>
              </React.Fragment>
            )}
          </div>

          {sceneTab === "active" && (
            <div className="arr-scene-tally">
              <span><i style={{ background: ARR_SCENE_STATE.done.dot }} />已完 {tallies.done}</span>
              <span><i style={{ background: ARR_SCENE_STATE.writing.dot }} />写中 {tallies.writing}</span>
              <span><i style={{ background: ARR_SCENE_STATE.todo.dot }} />待写 {tallies.todo}</span>
            </div>
          )}

          {sceneTab === "active" ? (
            <ul className="arr-scene-list">
              {ch.scenes.map((s, idx) => (
                <ArrSceneRow key={s.sid} s={s} n={String(idx + 1).padStart(2, "0")} picked={pickedScene === String(idx)}
                  onPick={() => setPickedScene(String(idx))} onCycleKind={() => onCycleKind(idx)}
                  onDelete={() => onDeleteScene(idx)} onEdit={(patch) => onEditScene(idx, patch)}
                  dragHandle={sceneDragHandle(idx)} dropZone={sceneDropZone(idx)} locked={locked}
                  selectMode={sceneSelectMode} selected={sceneSelectMode && sb.has && sb.has(s.sid)} onToggleSelect={sb.onToggle} />
              ))}
            </ul>
          ) : (
            <div className="arr-recycle-empty">
              <I.Trash size={18} />
              <span>已删除场景统一保存在服务端回收站，恢复后会重新拉取目录。</span>
              <button className="btn btn-quiet btn-sm" onClick={onOpenTrash}>打开回收站</button>
            </div>
          )}
        </section>

        {/* AI 编排：三方向候选 + 一键补全，咨询式补丁经作者逐条确认后原子回写目录 */}
        <ArrPlanPanel ch={ch} locked={locked} />
      </div>
    </section>
  );
}

/* ---- right context column ---- */
function ArrCheckRow({ ok, warn, label, val }) {
  const Ic = ok ? I.Check : warn ? I.AlertTriangle : I.Circle;
  return (
    <li className={`arr-check ${ok ? "is-ok" : warn ? "is-warn" : ""}`}>
      <Ic size={13} />
      <span className="arr-check-label">{label}</span>
      <span className="arr-check-val tab-num">{val}</span>
    </li>
  );
}

function ArrChapterContext({ ch, chapters, numOf, snow }) {
  const dramaKeys = ["promise", "spine", "arc", "problem", "aftertaste", "ending"];
  const dramaDone = dramaKeys.filter((k) => ch.drama[k] && !ch.drama[k].includes("（待")).length;
  const ready = ch.scenes.filter((s) => s.state === "done" || s.state === "writing").length;
  const pct = ch.words.target ? ch.words.cur / ch.words.target : 0;
  const budget = ch.words.cur === 0 ? { label: "未开始", warn: true } : pct < 0.85 ? { label: "进行", warn: true } : pct <= 1.12 ? { label: "在轨", ok: true } : { label: "超额", warn: true };
  const carry = ch.threads.filter((t) => t.role === "延续" || t.role === "新引").length;
  const woven = useMemoA(() => arrDeriveThreads(chapters, numOf), [chapters, numOf]);
  const wovenByName = useMemoA(() => Object.fromEntries(woven.map((t) => [t.name, t])), [woven]);
  const curCi = chapters.findIndex((c) => c.id === ch.id);

  return (
    <aside className="arr-ctx">
      <div className="ctx-block">
        <div className="ctx-head"><I.ShieldCheck size={13} /><span>章节体检</span></div>
        <ul className="arr-checks">
          <ArrCheckRow ok={dramaDone === 6} warn={dramaDone < 6} label="戏剧卡完整" val={`${dramaDone}/6`} />
          <ArrCheckRow ok={ready === ch.scenes.length} warn={ready < ch.scenes.length} label="场景就绪" val={`${ready}/${ch.scenes.length}`} />
          <ArrCheckRow ok={ch.align} warn={!ch.align} label="与上一章出口对齐" val={ch.align ? "已对齐" : "待校"} />
          <ArrCheckRow ok={budget.ok} warn={budget.warn} label="字数预算" val={budget.label} />
          <ArrCheckRow ok={carry === 0} warn={carry > 0} label="线索待交接" val={`${carry} 项`} />
        </ul>
      </div>

      {/* AI 体检：LLM findings 与上面的规则体检合流（规则免费兜底，AI 补结构性判断） */}
      <ArrAiHealthBlock ch={ch} locked={ch.state === "approved"} />

      <div className="ctx-block">
        <div className="ctx-head"><I.Activity size={13} /><span>字数预算</span></div>
        <ArrBudgetBar cur={ch.words.cur} target={ch.words.target} />
        <ul className="arr-meta">
          <li><span>目标</span><strong className="tab-num">{ch.words.target.toLocaleString()}</strong></li>
          <li><span>当前</span><strong className="tab-num">{ch.words.cur.toLocaleString()}</strong></li>
          <li><span>张力</span><strong className="tab-num">{Math.round(ch.tension * 100)}</strong></li>
        </ul>
      </div>

      <div className="ctx-block">
        <div className="ctx-head"><I.GitBranch size={13} /><span>线索</span><span className="arr-thread-hint">横条为全书走向 · 亮格＝本章</span></div>
        <ul className="arr-threads">
          {ch.threads.map((t, i) => {
            const wt = wovenByName[t.name];
            return (
              <li key={i}>
                <div className="arr-thread-top">
                  <span className="arr-thread-name">{t.name}</span>
                  <span className={`pill pill-${(ARR_THREAD_ROLE[t.role] || {}).tone || "slate"} text-xs`}><span className="pill-dot" />{t.role}</span>
                </div>
                {wt && <ArrThreadMini thread={wt} n={chapters.length} curCi={curCi} />}
              </li>
            );
          })}
        </ul>
      </div>

      <div className="ctx-block">
        <div className="ctx-head"><I.Eye size={13} /><span>视角 · 时空</span></div>
        <ul className="arr-meta">
          <li><span>POV</span><strong>{ch.pov}</strong></li>
          <li><span>时间</span><strong>{ch.time}</strong></li>
          <li><span>地点</span><strong>{ch.place}</strong></li>
        </ul>
      </div>

      <div className="ctx-block">
        <div className="ctx-head"><I.Snowflake size={13} /><span>从雪花同步</span></div>
        <p className="arr-sync">
          {(!snow || !snow.ready)
            ? "本作还没从「构思」物化过章节结构——先去构思把雪花大纲整理成章节，再回来同步。"
            : snow.pending
              ? `构思侧有 ${snow.pending} 场改动待回流到目录场景卡（三拍 / POV / 章 brief）。`
              : "目录已与构思同步。"}
          {snow && snow.note ? <span style={{ color: "var(--ink-2)" }}> · {snow.note}</span> : null}
        </p>
        <button className="btn btn-ghost btn-sm" style={{ width: "100%" }} onClick={snow && snow.onSync} disabled={snow && snow.busy}>
          <I.Refresh size={13} /> {snow && snow.busy ? "同步中…" : ((!snow || !snow.ready) ? "去构思物化" : (snow.pending ? `重新同步 · ${snow.pending} 场` : "重新同步"))}
        </button>
      </div>
    </aside>
  );
}

/* ---- detail left rail ---- */
function ArrRail({ chapters, numOf, pickedId, onPick, chDnd, boardDnd, onBack, onNew }) {
  const approved = chapters.filter((c) => c.state === "approved").length;
  const going = chapters.filter((c) => c.state === "writing" || c.state === "review" || c.state === "draft").length;
  const planned = chapters.filter((c) => c.state === "planned").length;
  const lastAct = ARR_ACTS[ARR_ACTS.length - 1].id;

  return (
    <aside className="arr-rail">
      <header className="arr-rail-head">
        <button className="arr-back" onClick={onBack}><I.Layers size={13} />全书编排</button>
        <h2 className="arr-rail-title text-serif">章节序列</h2>
      </header>
      <div className="arr-rail-stat">
        <span><strong className="tab-num">{approved}</strong> 批准</span>
        <span><strong className="tab-num">{going}</strong> 进行</span>
        <span><strong className="tab-num">{planned}</strong> 规划</span>
      </div>
      <div className="arr-rail-list">
        {ARR_ACTS.map((a) => {
          const items = chapters.filter((c) => c.act === a.id);
          return (
            <div className="arr-rail-act" key={a.id} {...boardDnd(a.id)}>
              <div className={`arr-rail-acthead tone-${a.tone}`}>{a.n}</div>
              <ul>
                {items.map((c) => {
                  const done = c.scenes.filter((s) => s.state === "done").length;
                  return (
                    <li key={c.id}>
                      <button className={`arr-rail-row ${c.id === pickedId ? "is-active" : ""}`} {...chDnd(c.id)} onClick={() => onPick(c.id)}>
                        <span className="arr-rail-grip"><I.GripVertical size={13} /></span>
                        <span className="arr-rail-num">{numOf[c.id]}</span>
                        <span className="arr-rail-body">
                          <span className="arr-rail-name text-serif">{c.title}</span>
                          <span className="arr-rail-meta"><ArrMiniScenes scenes={c.scenes} /><span className="tab-num">{done}/{c.scenes.length}</span></span>
                        </span>
                        <ArrChPill s={c.state} sm />
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
        <button className="arr-rail-new" onClick={() => onNew(lastAct)}><I.Plus size={14} /> 新建章节</button>
      </div>
    </aside>
  );
}

/* ==========================================================
   Shell
   ========================================================== */
function WsAuthor({ go }) {
  const catalogChapters = useCatalogChapters ? useCatalogChapters() : null;
  const [mode, setMode] = useStA(() => arrLsGet("arr.mode", "overview"));
  const [lens, setLens] = useStA(() => arrLsGet("arr.lens", "arc"));
  const [pickedId, setPickedId] = useStA(() => arrLsGet("arr.picked", "ch08"));
  const [sceneTab, setSceneTab] = useStA("active");
  const [pickedScene, setPickedScene] = useStA("0");
  const [chapters, setChapters] = useStA(() => {
    // 本地 state 只负责拖拽中的即时视图；服务端目录始终是单一真相源。
    if (WsCatalog) return arrStampIds(catalogChapters || WsCatalog.get());
    const saved = arrLsGet("arr.chapters", null);
    return arrStampIds(Array.isArray(saved) && saved.length ? saved : []);
  });
  const [chDragId, setChDragId] = useStA(null);
  const [scDragIdx, setScDragIdx] = useStA(null);
  /* 批量删除的选择集（只活在本次会话里，不落 localStorage：
     选择是瞬时意图，跨刷新保留只会让作者对着一份看不见来路的勾选下手） */
  const [chSelectMode, setChSelectMode] = useStA(false);
  const [chSel, setChSel] = useStA(() => new Set());
  const [scSelectMode, setScSelectMode] = useStA(false);
  const [scSel, setScSel] = useStA(() => new Set());
  const { toast, show: showNotice, clear: clearNotice } = useUndoToast();
  const chaptersRef = useRefA(chapters);
  chaptersRef.current = chapters;

  useEfA(() => arrLsSet("arr.mode", mode), [mode]);
  useEfA(() => arrLsSet("arr.lens", lens), [lens]);
  useEfA(() => arrLsSet("arr.picked", pickedId), [pickedId]);
  useEfA(() => {
    if (!WsCatalog) return;
    const next = arrStampIds(Array.isArray(catalogChapters) ? catalogChapters : []);
    setChapters(next);
    setPickedId((current) => {
      if (next.some((item) => item.id === current)) return current;
      const fallback = next.find((item) => item.current) || next[0];
      return fallback ? fallback.id : null;
    });
    /* 目录换了：把指向已消失章/场的勾选摘掉，避免对着幽灵 id 再发一次删除 */
    const liveCh = new Set(next.map((item) => item.id));
    const liveSc = new Set(next.flatMap((item) => (item.scenes || []).map((s) => s.sid)));
    const prune = (live) => (prevSel) => {
      const kept = new Set([...prevSel].filter((id) => live.has(id)));
      return kept.size === prevSel.size ? prevSel : kept;
    };
    setChSel(prune(liveCh));
    setScSel(prune(liveSc));
  }, [catalogChapters]);

  const commitChapters = (recipe) => {
    const base = WsCatalog ? arrStampIds(WsCatalog.get()) : chapters;
    const next = arrStampIds(typeof recipe === "function" ? recipe(base) : recipe);
    setChapters(next);
    if (WsCatalog) WsCatalog.set(next);
    else arrLsSet("arr.chapters", next);
    return next;
  };
  const approvedPositionsStayFixed = (before, after) => before.every((chapter, index) => (
    chapter.state !== "approved" || (after[index] && after[index].id === chapter.id)
  ));
  const publishDraggedOrder = () => {
    const next = arrStampIds(chaptersRef.current);
    if (WsCatalog) WsCatalog.set(next);
    else arrLsSet("arr.chapters", next);
  };

  const byId = useMemoA(() => Object.fromEntries(chapters.map((c) => [c.id, c])), [chapters]);
  const numOf = useMemoA(() => Object.fromEntries(chapters.map((c, i) => [c.id, String(i + 1).padStart(2, "0")])), [chapters]);

  /* —— 从雪花同步（回流）：把构思 9/10 步的改动写回本作目录场景卡（三拍 / POV / 章 brief）。
     能力来自全局 SnowSync（与构思页「重新同步」同源）；resync 内部已重拉 WsCatalog，
     这里再把最新目录灌回本页 chapters。pending 来自后端 resync_status（真相），不再写死。
     ⚠ 这些 hook 必须在下方「空作品」early-return 之前调用（React hooks 顺序规则）。 */
  const readSnowResync = () => { try { return (window.SnowSync && window.SnowSync.resyncStatus()) || { pendingCount: 0 }; } catch (e) { return { pendingCount: 0 }; } };
  const [snowResync, setSnowResync] = useStA(readSnowResync);
  const [snowBusy, setSnowBusy] = useStA(false);
  const [snowNote, setSnowNote] = useStA(null);   // 最近一次同步结果的内联提示
  useEfA(() => {
    const refresh = () => setSnowResync(readSnowResync());
    window.addEventListener("ws:snow-resync", refresh);
    window.addEventListener("ws:snow-hydrated", refresh);
    window.addEventListener("ws:work-changed", refresh);
    return () => { window.removeEventListener("ws:snow-resync", refresh); window.removeEventListener("ws:snow-hydrated", refresh); window.removeEventListener("ws:work-changed", refresh); };
  }, []);
  const snowReady = (() => { try { return !!(window.SnowSync && window.SnowSync.readyToMaterialize && window.SnowSync.readyToMaterialize()); } catch (e) { return false; } })();
  const syncFromSnow = async () => {
    if (snowBusy) return;
    if (!window.SnowSync || !window.SnowSync.resync) { window.alert("同步能力尚未就绪——请刷新页面，或先到「构思」把雪花大纲整理成章节结构。"); return; }
    if (!snowReady) {   // 从没走过物化主路径：暂无可回流的场，引导去构思页
      if (window.confirm("这部作品还没从「构思」物化过章节结构，暂无可回流的场。\n\n现在去构思页把雪花大纲整理成章节结构吗？")) location.hash = "#snowflake";
      return;
    }
    setSnowBusy(true); setSnowNote(null);
    try {
      const r = await window.SnowSync.resync();                  // POST /resync，内部已 WsCatalog.__refresh
      if (WsCatalog) setChapters(arrStampIds(WsCatalog.get()));  // 订阅也会收敛；这里让提示即时可见
      setSnowResync(readSnowResync());
      setSnowNote((r && r.synced) ? `已同步 ${r.synced} 场的构思改动到目录` : "目录已是最新，无需同步");
    } catch (e) {
      window.alert("从雪花同步失败：" + ((e && e.message) || "请稍后重试，或检查构思各步是否已确认。"));
    } finally { setSnowBusy(false); }
  };
  const snow = { pending: (snowResync && snowResync.pendingCount) || 0, busy: snowBusy, ready: snowReady, note: snowNote, onSync: syncFromSnow };

  if (WsCatalog && !WsCatalog.ready()) {
    const catalogError = WsCatalog.loadError && WsCatalog.loadError();
    return (
      <div className="page" data-screen-label="author · loading">
        <div style={{ display: "grid", placeItems: "center", minHeight: "60vh", color: "var(--ink-3)" }} role="status">
          {catalogError ? (
            <div style={{ display: "grid", justifyItems: "center", gap: 12, maxWidth: 460, textAlign: "center" }}>
              <strong style={{ color: "var(--ink-1)" }}>章节目录加载失败</strong>
              <span>系统不会把请求失败误判成空作品，也不会用空目录覆盖服务端。</span>
              <button className="btn btn-ghost" onClick={() => WsCatalog.__refresh && WsCatalog.__refresh()}><I.Refresh size={13} /> 重试加载</button>
            </div>
          ) : "正在从服务端加载章节目录…"}
        </div>
      </div>
    );
  }

  /* 空白作品：服务端已确认没有任何章节，先引导建立结构 */
  if (!chapters.length) {
    const createFirst = () => {
      if (!WsCatalog) return;
      WsCatalog.addChapter();
    };
    return (
      <div className="page" data-screen-label="author · empty">
        <div style={{ display: "grid", placeItems: "center", minHeight: "60vh", textAlign: "center" }}>
          <div style={{ maxWidth: 440, display: "grid", gap: 14, justifyItems: "center" }}>
            <div style={{ fontFamily: "var(--font-serif)", fontSize: 22, color: "var(--ink-1)" }}>这部作品还没有章节结构</div>
            <p style={{ color: "var(--ink-3)", fontSize: 14, lineHeight: 1.8, margin: 0 }}>
              章节编排从第一章开始；也可以先去雪花构思，把大纲长出来再回来编排。
            </p>
            <div style={{ display: "flex", gap: 10 }}>
              <button className="btn btn-accent" onClick={createFirst}><I.Plus size={15} /> 新建第一章</button>
              <button className="btn btn-ghost" onClick={() => { location.hash = "#snowflake"; }}>去构思</button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const ch = byId[pickedId] || chapters[0];
  const idx = chapters.findIndex((c) => c.id === ch.id);
  const prev = idx > 0 ? chapters[idx - 1] : null;
  const next = idx >= 0 && idx < chapters.length - 1 ? chapters[idx + 1] : null;

  const openChapter = (id) => {
    setPickedId(id); setPickedScene("0"); setSceneTab("active"); setMode("detail");
    setScSelectMode(false); setScSel(new Set());   // 换章即清空场景勾选，避免跨章误删
  };

  /* chapter drag — cross-volume: dragged chapter adopts the target's act + position */
  const chDnd = (id) => ({
    draggable: (chapters.find((item) => item.id === id) || {}).state !== "approved",
    onDragStart: (e) => {
      if ((chapters.find((item) => item.id === id) || {}).state === "approved") { e.preventDefault(); return; }
      setChDragId(id); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", id); } catch (_) {}
    },
    onDragEnter: () => {
      if (!chDragId || chDragId === id) return;
      if ((chapters.find((item) => item.id === chDragId) || {}).state === "approved") return;
      setChapters((cs) => {
        const from = cs.findIndex((c) => c.id === chDragId);
        const to = cs.findIndex((c) => c.id === id);
        if (from < 0 || to < 0 || from === to) return cs;
        const arr = [...cs];
        const moved = { ...arr[from], act: arr[to].act };
        arr.splice(from, 1);
        arr.splice(arr.findIndex((c) => c.id === id), 0, moved);
        if (!approvedPositionsStayFixed(cs, arr)) return cs;
        chaptersRef.current = arr;
        return arr;
      });
    },
    onDragOver: (e) => e.preventDefault(),
    onDrop: (e) => { e.preventDefault(); publishDraggedOrder(); setChDragId(null); },
    onDragEnd: () => { publishDraggedOrder(); setChDragId(null); },
    "data-dragging": chDragId === id ? "true" : undefined,
  });
  /* drop on an act's empty space → move dragged chapter to the end of that volume */
  const boardDnd = (actId) => ({
    onDragOver: (e) => e.preventDefault(),
    onDrop: (e) => {
      e.preventDefault();
      const dragId = chDragId;
      setChDragId(null);
      if (!dragId) return;
      const cs = chaptersRef.current;
      {
        const cur = cs.find((c) => c.id === dragId);
        if (!cur || cur.state === "approved" || cur.act === actId) return;
        const arr = cs.filter((c) => c.id !== dragId);
        let insertAt = arr.length;
        for (let i = arr.length - 1; i >= 0; i--) { if (arr[i].act === actId) { insertAt = i + 1; break; } }
        arr.splice(insertAt, 0, { ...cur, act: actId });
        if (!approvedPositionsStayFixed(cs, arr)) return;
        chaptersRef.current = arr;
        commitChapters(arr);
      }
    },
  });

  /* scene drag within the current chapter */
  const sceneDragHandle = (i) => ({
    draggable: ch.state !== "approved",
    onDragStart: (e) => {
      if (ch.state === "approved") { e.preventDefault(); return; }
      setScDragIdx(i); e.dataTransfer.effectAllowed = "move"; e.stopPropagation();
    },
    onDragEnd: () => { publishDraggedOrder(); setScDragIdx(null); },
  });
  const sceneDropZone = (i) => ({
    onDragEnter: () => {
      if (scDragIdx == null || scDragIdx === i) return;
      if (ch.state === "approved") return;
      setChapters((cs) => {
        const next = cs.map((c) => {
        if (c.id !== ch.id) return c;
        const s = [...c.scenes]; const m = s.splice(scDragIdx, 1)[0]; s.splice(i, 0, m);
        return { ...c, scenes: s };
        });
        chaptersRef.current = next;
        return next;
      });
      setScDragIdx(i);
    },
    onDragOver: (e) => e.preventDefault(),
    onDrop: (e) => { e.preventDefault(); publishDraggedOrder(); setScDragIdx(null); },
    "data-dragging": scDragIdx === i ? "true" : undefined,
  });

  const addChapter = (actId) => {
    const id = "ch_" + Math.random().toString(36).slice(2, 8);
    commitChapters((cs) => {
      const newCh = {
        id, act: actId, n: "", title: "未命名章节", state: "planned",
        tension: 0.5, pov: "", time: "待定", place: "待定",
        words: { cur: 0, target: 4000 },
        entry: "（待规划）", exit: "（待规划）", align: true, promise: "",
        drama: { promise: "", spine: "", arc: "", problem: "", aftertaste: "", ending: "",
          forbidden: "不得出现梦醒、系统提示、参考书专名、原书人物或可识别桥段。", notes: "新建章节 · 待补。" },
        threads: [], scenes: [{ sid: "s_" + Math.random().toString(36).slice(2, 8), title: "未命名场景", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" }],
      };
      const arr = [...cs];
      let insertAt = arr.length;
      for (let i = arr.length - 1; i >= 0; i--) { if (arr[i].act === actId) { insertAt = i + 1; break; } }
      arr.splice(insertAt, 0, newCh);
      return arr;
    });
    setPickedId(id); setPickedScene("0"); setSceneTab("active"); setMode("detail");
  };
  const addScene = () => {
    if (ch.state === "approved") return;
    commitChapters((cs) => cs.map((c) => c.id === ch.id
      ? { ...c, scenes: [...c.scenes, { sid: "s_" + Math.random().toString(36).slice(2, 8), title: "未命名场景", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" }] }
      : c));
    setSceneTab("active");
  };

  /* write-back + status helpers */
  const patchTitle = (val) => { if (ch.state !== "approved") commitChapters((cs) => cs.map((c) => c.id === ch.id ? { ...c, title: val } : c)); };
  const patchDrama = (key, val) => { if (ch.state !== "approved") commitChapters((cs) => cs.map((c) => {
    if (c.id !== ch.id) return c;
    const drama = { ...c.drama, [key]: val };
    const next = { ...c, drama };
    if (key === "promise") next.promise = val;
    return next;
  })); };
  const editScene = (i, patch) => { if (ch.state !== "approved") commitChapters((cs) => cs.map((c) => {
    if (c.id !== ch.id) return c;
    return { ...c, scenes: c.scenes.map((sc, idx) => idx === i ? { ...sc, ...patch } : sc) };
  })); };
  const cycleKind = (i) => { if (ch.state !== "approved") commitChapters((cs) => cs.map((c) => {
    if (c.id !== ch.id) return c;
    return { ...c, scenes: c.scenes.map((sc, idx) => idx === i ? { ...sc, kind: sc.kind === "主动" ? "反应" : "主动" } : sc) };
  })); };
  const deleteScene = (i) => {
    if (ch.state === "approved") return;
    const victim = ch.scenes[i];
    commitChapters((cs) => cs.map((c) => {
      if (c.id !== ch.id) return c;
      return { ...c, scenes: c.scenes.filter((_, idx) => idx !== i) };
    }));
    noticeTrashed(`已把场景「${(victim && victim.title) || "未命名场景"}」移入回收站`);
  };
  const deleteChapter = () => {
    if (ch.state === "approved") return;
    const scenes = (ch.scenes || []).length;
    if (typeof window !== "undefined" && !window.confirm(`把第 ${numOf[ch.id]} 章「${ch.title}」移入回收站？${scenes ? `连同章下 ${scenes} 个场景。` : ""}`)) return;
    const i = chapters.findIndex((c) => c.id === ch.id);
    const neighbor = chapters[i + 1] || chapters[i - 1];
    commitChapters((cs) => cs.filter((c) => c.id !== ch.id));
    if (neighbor) { setPickedId(neighbor.id); setPickedScene("0"); }
    noticeTrashed(`已把第 ${numOf[ch.id]} 章移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
  };

  /* —— 批量删除 ——
     已批准终稿始终排除在选择之外：后端也会 blocked，这里先在 UI 上说清楚，
     免得作者勾了一批、只删掉一部分还得自己对账。
     删完给一条回执（删了几条 / 去了哪 / 怎么找回）——不然作者只能看着东西消失。 */
  const noticeTrashed = (text) => showNotice({
    text,
    actionLabel: "打开回收站",
    onAction: () => { if (go) go("trash"); else location.hash = "#trash"; },
  });
  const selectableChapters = chapters.filter((c) => c.state !== "approved");
  const toggleChSel = (id) => setChSel((prev) => {
    const nextSel = new Set(prev);
    if (nextSel.has(id)) nextSel.delete(id); else nextSel.add(id);
    return nextSel;
  });
  const selectedChapters = selectableChapters.filter((c) => chSel.has(c.id));
  const chapterBatch = {
    mode: chSelectMode,
    has: (id) => chSel.has(id),
    onToggle: toggleChSel,
    selectedCount: selectedChapters.length,
    selectableCount: selectableChapters.length,
    allSelected: !!selectableChapters.length && selectedChapters.length === selectableChapters.length,
    onSelectAll: () => setChSel((prev) => (
      prev.size === selectableChapters.length ? new Set() : new Set(selectableChapters.map((c) => c.id))
    )),
    onExit: () => { setChSelectMode(false); setChSel(new Set()); },
    onDelete: () => {
      const targets = selectedChapters;
      if (!targets.length) return;
      const scenes = targets.reduce((n, c) => n + (c.scenes || []).length, 0);
      const what = targets.length === 1
        ? `第 ${numOf[targets[0].id]} 章「${targets[0].title}」`
        : `所选 ${targets.length} 章`;
      if (typeof window !== "undefined" && !window.confirm(`把${what}移入回收站？${scenes ? `连同章下 ${scenes} 个场景。` : ""}`)) return;
      const drop = new Set(targets.map((c) => c.id));
      const survivor = chapters.find((c) => !drop.has(c.id));
      commitChapters((cs) => cs.filter((c) => !drop.has(c.id)));
      setChSel(new Set());
      setChSelectMode(false);
      if (drop.has(pickedId)) { setPickedId(survivor ? survivor.id : null); setPickedScene("0"); }
      noticeTrashed(`已把 ${targets.length} 章移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
    },
  };
  const sceneBatch = {
    mode: scSelectMode,
    has: (sid) => scSel.has(sid),
    onToggleMode: () => { setScSelectMode((v) => !v); setScSel(new Set()); },
    onToggle: (sid) => setScSel((prev) => {
      const nextSel = new Set(prev);
      if (nextSel.has(sid)) nextSel.delete(sid); else nextSel.add(sid);
      return nextSel;
    }),
    onSelectAll: () => setScSel((prev) => (
      prev.size === ch.scenes.length ? new Set() : new Set(ch.scenes.map((s) => s.sid))
    )),
    selectedCount: ch.scenes.filter((s) => scSel.has(s.sid)).length,
    onDelete: () => {
      if (ch.state === "approved") return;
      const targets = ch.scenes.filter((s) => scSel.has(s.sid));
      if (!targets.length) return;
      const what = targets.length === 1 ? `场景「${targets[0].title}」` : `本章 ${targets.length} 个场景`;
      if (typeof window !== "undefined" && !window.confirm(`把${what}移入回收站？`)) return;
      const drop = new Set(targets.map((s) => s.sid));
      commitChapters((cs) => cs.map((c) => (c.id !== ch.id ? c : { ...c, scenes: c.scenes.filter((s) => !drop.has(s.sid)) })));
      setScSel(new Set());
      setScSelectMode(false);
      setPickedScene("0");
      noticeTrashed(`已把 ${targets.length} 个场景移入回收站`);
    },
  };
  const refreshData = async () => {
    if (!WsCatalog || !WsCatalog.__refresh) return;
    await WsCatalog.__refresh();
    const failure = WsCatalog.loadError && WsCatalog.loadError();
    if (failure) {
      window.alert((failure && failure.message) || "章节目录刷新失败；当前视图仍保留上一次服务端版本。");
      return;
    }
    const fresh = arrStampIds(WsCatalog.get());
    setChapters(fresh);
    const selected = fresh.find((c) => c.id === pickedId) || fresh.find((c) => c.current) || fresh[0];
    setPickedId(selected ? selected.id : null);
  };
  const chapterRun = {
    onCatalogRefresh: (next) => setChapters(arrStampIds(Array.isArray(next) ? next : [])),
    onOpenReview: () => {
      if (go) go("manuscripts");
      else if (typeof window !== "undefined") window.location.hash = "#manuscripts";
    },
    onConfigureModel: () => {
      if (go) go("settings");
      else if (typeof window !== "undefined") window.location.hash = "#settings";
    },
  };
  const firstAct = ARR_ACTS[0].id;

  return (
    <div className="arr-shell" data-screen-label="author" data-mode={mode} data-dragging-ch={chDragId ? "true" : undefined}>
      <div className={`arr-main ${mode === "overview" ? "arr-main-ov" : "arr-main-detail"}`}>
        {mode === "overview" ? (
          <React.Fragment>
            {/* 多选是一个独占模式：进入后头部只剩这条批量操作栏，
                页面上不再同时摆着「新建 / 切视图 / 逐章打开」等与选择无关的动作 */}
            <header className={`arr-ov-head ${chSelectMode ? "is-selecting" : ""}`}>
              {chSelectMode ? (
                <div className="arr-batch" role="toolbar" aria-label="章节批量操作">
                  <span className="arr-batch-n">已选 <strong className="tab-num">{chapterBatch.selectedCount}</strong> / {chapterBatch.selectableCount} 章</span>
                  <span className="arr-batch-hint">点卡片选中；已批准终稿不可选。删除后进入回收站，可恢复。</span>
                  <button className="btn btn-quiet btn-sm" onClick={chapterBatch.onSelectAll} disabled={!chapterBatch.selectableCount}>
                    {chapterBatch.allSelected ? "取消全选" : "全选"}
                  </button>
                  <button className="btn btn-danger btn-sm" data-testid="author-batch-delete-chapters"
                    disabled={!chapterBatch.selectedCount} onClick={chapterBatch.onDelete}>
                    <I.Trash size={13} /> 删除所选{chapterBatch.selectedCount ? ` · ${chapterBatch.selectedCount} 章` : ""}
                  </button>
                  <button className="btn btn-ghost btn-sm" data-testid="author-chapter-select-exit" onClick={chapterBatch.onExit}>完成</button>
                </div>
              ) : (
                <React.Fragment>
                  <div>
                    <div className="page-eyebrow" style={{ margin: 0 }}>章节编排</div>
                    <h1 className="arr-ov-title text-serif">全书编排 · {WsWorks ? WsWorks.active().title : "未命名作品"}</h1>
                  </div>
                  <div className="arr-ov-head-r">
                    <button className="btn btn-quiet btn-sm" onClick={refreshData} title="从服务端重新载入目录"><I.Refresh size={13} /> 刷新</button>
                    <div className="seg">
                      <button className="seg-btn is-active" disabled title="当前正在查看全书编排">全书编排</button>
                      <button className="seg-btn" onClick={() => setMode("detail")}>章节详情</button>
                    </div>
                    <button className="btn btn-quiet btn-sm" data-testid="author-chapter-select-mode"
                      title="多选章节后可一次删除（已批准终稿不可选）"
                      disabled={!chapterBatch.selectableCount}
                      onClick={() => { setChSelectMode(true); setChSel(new Set()); }}>
                      <I.Check size={13} /> 多选
                    </button>
                    <button className="btn btn-accent btn-sm" onClick={() => addChapter(firstAct)}><I.Plus size={13} /> 新建章节</button>
                  </div>
                </React.Fragment>
              )}
            </header>
            <ArrOverview chapters={chapters} numOf={numOf} pickedId={pickedId} onOpen={openChapter} chDnd={chDnd} boardDnd={boardDnd} onNew={addChapter} lens={lens} setLens={setLens} batch={chapterBatch} />
          </React.Fragment>
        ) : (
          <React.Fragment>
            <ArrRail chapters={chapters} numOf={numOf} pickedId={pickedId} onPick={openChapter} chDnd={chDnd} boardDnd={boardDnd} onBack={() => setMode("overview")} onNew={addChapter} />
            <ArrEditor ch={ch} num={numOf[ch.id]} prev={prev} next={next} numOf={numOf} sceneTab={sceneTab} setSceneTab={setSceneTab}
              sceneDragHandle={sceneDragHandle} sceneDropZone={sceneDropZone} onAddScene={addScene} onCycleKind={cycleKind}
              onDeleteScene={deleteScene} onEditScene={editScene}
              onPatchTitle={patchTitle} onPatchDrama={patchDrama} onDeleteChapter={deleteChapter}
              onOpenTrash={() => { if (go) go("trash"); else location.hash = "#trash"; }}
              pickedScene={pickedScene} setPickedScene={setPickedScene}
              onJump={openChapter} onBack={() => setMode("overview")} snow={snow} chapterRun={chapterRun} sceneBatch={sceneBatch} />
            <ArrChapterContext ch={ch} chapters={chapters} numOf={numOf} snow={snow} />
          </React.Fragment>
        )}
      </div>
      <UndoToast toast={toast} onClose={clearNotice} />
    </div>
  );
}


/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { WsAuthor };
