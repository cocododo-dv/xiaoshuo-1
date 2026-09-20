import React from "react";
import { I } from "./icons.jsx";
import { ARR_ACTS, ARR_CH_STATE, ARR_SCENE_STATE } from "./ws-author-data.jsx";
import { arrActSpans } from "./ws-author-loom.jsx";

/* global React, I, ARR_ACTS, ARR_CH_STATE, ARR_SCENE_STATE, arrActSpans */

/* ==========================================================
   节奏镜头 — Pacing Lens
   按章字数直方图（实填 vs 目标虚影）+ POV 着色 + POV 泳道
   + 场景密度 + 时间轴。一眼看出哪一章注水、哪一章太薄、
   POV 切换是否健康。
   ========================================================== */

const PACE_TONES = ["crimson", "gold", "sage", "slate", "rose"];

/* 一章的 POV：章节编排传进来的是「镜头用的章」（arrLensChapters）——章级 POV 没填时 pov = 本章场次最多的
   那一位、povs = 本章各场出现过的全部视角。旧调用方只有 pov 也照常工作。 */
const pacePovs = (c) => (Array.isArray(c.povs) && c.povs.length ? c.povs : [c.pov]);

function arrPovMap(chapters) {
  const m = {}; let i = 0;
  chapters.forEach((c) => pacePovs(c).forEach((p) => { if (!(p in m)) { m[p] = PACE_TONES[i % PACE_TONES.length]; i++; } }));
  return m;
}

function ArrPaceDots({ scenes }) {
  return (
    <span className="pace-dots" title={`${scenes.length} 场`}>
      {scenes.map((s, i) => (
        <i key={i} style={{ background: (ARR_SCENE_STATE[s.state] || ARR_SCENE_STATE.todo).dot }} />
      ))}
    </span>
  );
}

function ArrPacingLens({ chapters, numOf, onOpen }) {
  const { useMemo: useMemoP } = React;
  const pov = useMemoP(() => arrPovMap(chapters), [chapters]);
  const bands = useMemoP(() => arrActSpans(chapters), [chapters]);
  const n = chapters.length;
  const maxV = Math.max(...chapters.map((c) => Math.max(c.words.target, c.words.cur)), 1);
  const totalCur = chapters.reduce((s, c) => s + c.words.cur, 0);
  const drafted = chapters.filter((c) => c.words.cur > 0);
  const avg = drafted.length ? Math.round(totalCur / drafted.length) : 0;
  const avgPct = (avg / maxV) * 100;

  const povCounts = {};
  chapters.forEach((c) => pacePovs(c).forEach((p) => { povCounts[p] = (povCounts[p] || 0) + 1; }));
  const povList = Object.keys(povCounts);

  // pacing outliers among drafted chapters
  const sorted = [...drafted].sort((a, b) => b.words.cur - a.words.cur);
  const fattest = sorted[0];
  const thinnest = sorted[sorted.length - 1];

  const gridVars = { "--loom-cols": n };

  return (
    <div className="pace">
      <div className="loom-summary">
        <span className="loom-sum-item"><strong className="tab-num">{avg.toLocaleString()}</strong> 字 · 已写章均长</span>
        <span className="loom-sum-sep" />
        {fattest && <span className="loom-sum-item loom-sum-long" style={{ marginLeft: 0 }}>最长 · <b>{numOf[fattest.id]} {fattest.title}</b> <span className="tab-num">{fattest.words.cur.toLocaleString()}</span></span>}
        {thinnest && fattest && thinnest.id !== fattest.id && <span className="loom-sum-item loom-sum-long" style={{ marginLeft: 0 }}>最短 · <b>{numOf[thinnest.id]} {thinnest.title}</b> <span className="tab-num">{thinnest.words.cur.toLocaleString()}</span></span>}
        <span className="loom-legend" style={{ marginLeft: "auto" }}>
          {povList.map((p) => (
            <span key={p}><i className={`loom-lg tone-fill-${pov[p]}`} />{p} <span className="tab-num">{povCounts[p]}</span></span>
          ))}
        </span>
      </div>

      <div className="pace-grid" style={gridVars}>
        {/* act bands */}
        <div className="loom-row loom-acts">
          <div className="pace-corner" />
          {bands.map((b) => (
            <div key={b.a.id} className={`loom-act tone-${b.a.tone}`} style={{ gridColumn: `${b.from + 2} / ${b.to + 3}` }}>
              <span className="loom-act-n">{b.a.n}</span><span className="loom-act-name">{b.a.name}</span>
            </div>
          ))}
        </div>

        {/* histogram */}
        <div className="loom-row pace-bars">
          <div className="pace-axis">
            <span>{(maxV / 1000).toFixed(1)}k</span>
            <span>0</span>
          </div>
          {avg > 0 && (
            <div className="pace-avgwrap" style={{ gridColumn: "2 / -1" }}>
              <span className="pace-avgline" style={{ bottom: avgPct + "%" }}><i>均 {avg.toLocaleString()}</i></span>
            </div>
          )}
          {chapters.map((c, ci) => {
            /* 没设目标的章（雪花整理出来的章都没有）不画目标虚影、也不谈超额 */
            const th = c.words.target > 0 ? Math.max(2, (c.words.target / maxV) * 100) : 0;
            const fh = (c.words.cur / maxV) * 100;
            const over = c.words.target > 0 && c.words.cur > c.words.target * 1.08;
            return (
              <button key={c.id} className="pace-barcell" style={{ gridColumn: ci + 2 }} onClick={() => onOpen(c.id)}
                title={`第 ${numOf[c.id]} 章 · ${c.title}\n${c.words.cur.toLocaleString()}${c.words.target > 0 ? ` / ${c.words.target.toLocaleString()}` : ""} 字 · POV ${pacePovs(c).join(" / ")}`}>
                <span className="pace-ghost" style={{ height: th + "%" }} />
                <span className={`pace-fill tone-fill-${pov[c.pov]} ${c.words.cur === 0 ? "is-empty" : ""} ${over ? "is-over" : ""}`} style={{ height: Math.max(c.words.cur === 0 ? 0 : 2, fh) + "%" }} />
              </button>
            );
          })}
        </div>

        {/* per-chapter footer: num · scenes · time */}
        <div className="loom-row pace-foot">
          <div className="pace-corner" />
          {chapters.map((c, ci) => (
            <button key={c.id} className="pace-col" style={{ gridColumn: ci + 2 }} onClick={() => onOpen(c.id)}>
              <span className={`pace-num ${c.current ? "is-current" : ""}`}>{numOf[c.id]}</span>
              <ArrPaceDots scenes={c.scenes} />
              <span className="pace-time">{c.time}</span>
            </button>
          ))}
        </div>

        {/* POV swimlanes */}
        <div className="pace-lanes">
          <div className="pace-lanes-label">POV 泳道</div>
          {povList.map((p) => (
            <div key={p} className="loom-row pace-lane" style={gridVars}>
              <div className={`pace-lane-name tone-${pov[p]}`}>{p}</div>
              {chapters.map((c, ci) => {
                const on = pacePovs(c).includes(p);
                // run edges for rounded segment ends
                const prevOn = ci > 0 && pacePovs(chapters[ci - 1]).includes(p);
                const nextOn = ci < n - 1 && pacePovs(chapters[ci + 1]).includes(p);
                return (
                  <span key={c.id} className={`pace-lane-cell ${on ? "is-on tone-fill-" + pov[p] : ""} ${on && !prevOn ? "is-start" : ""} ${on && !nextOn ? "is-end" : ""}`}
                    style={{ gridColumn: ci + 2 }} />
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { arrPovMap, ArrPacingLens };
