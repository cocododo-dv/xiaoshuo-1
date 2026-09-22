import React from "react";
import { ARR_SCENE_STATE } from "./ws-author-data.jsx";
import { arrActSpans } from "./ws-author-derive.js";
import { chapterLabel } from "./ws-labels.js";

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

/* 泳道名字那一列：所有行共用一个宽度（每一行是各自的 grid，max-content 会让各行对不齐），
   按最长的名字估，夹在 64–160px 之间；更长的名字省略，整名在悬停提示里。 */
function paceNameWidth(names) {
  const longest = names.reduce((n, name) => Math.max(n, String(name || "").length), 0);
  return Math.min(160, Math.max(64, longest * 13 + 14));
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
  const pov = React.useMemo(() => arrPovMap(chapters), [chapters]);
  const bands = React.useMemo(() => arrActSpans(chapters), [chapters]);
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

  const gridVars = { "--pace-cols": n, "--pace-name": paceNameWidth(povList) + "px" };
  // 「第 3 章 · 盐场」；占位章名（第 N 章 / 未命名）只写章号
  const chName = (c, maxTitle = 12) => chapterLabel({ n: numOf[c.id], title: c.title }, { maxTitle });

  return (
    <div className="pace">
      <div className="arr-lens-summary">
        <span className="arr-lens-sum-item"><strong className="tab-num">{avg.toLocaleString()}</strong> 字 · 已写章均长</span>
        {fattest && <span className="arr-lens-sum-item">最长 <b>{chName(fattest)}</b> <span className="tab-num">{fattest.words.cur.toLocaleString()}</span></span>}
        {thinnest && fattest && thinnest.id !== fattest.id && <span className="arr-lens-sum-item">最短 <b>{chName(thinnest)}</b> <span className="tab-num">{thinnest.words.cur.toLocaleString()}</span></span>}
        <span className="arr-lens-legend">
          {povList.map((p) => (
            <span key={p} title={`${p}：${povCounts[p]} 章里有这一视角`}><i className={`arr-lens-lg tone-fill-${pov[p]}`} />{p} <span className="tab-num">{povCounts[p]}</span></span>
          ))}
        </span>
      </div>

      <div className="pace-grid" style={gridVars}>
        {/* act bands */}
        <div className="pace-row pace-acts">
          <div className="pace-corner" />
          {bands.map((b) => (
            <div key={b.a.id} className="pace-act" data-tone={b.a.tone} style={{ gridColumn: `${b.from + 2} / ${b.to + 3}` }}>{b.a.n}</div>
          ))}
        </div>

        {/* histogram */}
        <div className="pace-row pace-bars">
          <div className="pace-axis" aria-hidden="true">
            <span>{(maxV / 1000).toFixed(1)}k</span>
            <span>0</span>
          </div>
          {avg > 0 && (
            <div className="pace-avgwrap" style={{ gridColumn: "2 / -1" }}>
              {/* 均线贴着顶（均长 ≈ 最长）时字标放到线下面，免得顶到上面的卷带 */}
              <span className={`pace-avgline ${avgPct > 88 ? "is-high" : ""}`} style={{ bottom: avgPct + "%" }}><i>均 {avg.toLocaleString()}</i></span>
            </div>
          )}
          {chapters.map((c, ci) => {
            /* 没设目标的章（雪花整理出来的章都没有）不画目标虚影、也不谈超额 */
            const th = c.words.target > 0 ? Math.max(2, (c.words.target / maxV) * 100) : 0;
            const fh = (c.words.cur / maxV) * 100;
            const over = c.words.target > 0 && c.words.cur > c.words.target * 1.08;
            const tip = `${chName(c, Infinity)}\n${c.words.cur.toLocaleString()}${c.words.target > 0 ? ` / ${c.words.target.toLocaleString()}` : ""} 字 · 视角 ${pacePovs(c).join(" / ")}`;
            return (
              <button type="button" key={c.id} className="pace-barcell" style={{ gridColumn: ci + 2 }} onClick={() => onOpen(c.id)}
                title={tip} aria-label={tip.split("\n")[0]}>
                <span className="pace-ghost" style={{ height: th + "%" }} />
                <span className={`pace-fill tone-fill-${pov[c.pov]} ${c.words.cur === 0 ? "is-empty" : ""} ${over ? "is-over" : ""}`} style={{ height: Math.max(c.words.cur === 0 ? 0 : 2, fh) + "%" }} />
              </button>
            );
          })}
        </div>

        {/* per-chapter footer: num · scenes · time */}
        <div className="pace-row pace-foot">
          <div className="pace-corner" />
          {chapters.map((c, ci) => (
            <button type="button" key={c.id} className="pace-col" style={{ gridColumn: ci + 2 }} onClick={() => onOpen(c.id)}
              aria-label={`打开${chName(c, Infinity)}`} title={chName(c, Infinity)}>
              {/* 每章一列，放不下「第 N 章」：列脚只写紧凑的章号，完整叫法在提示与无障碍名里 */}
              <span className={`pace-num tab-num ${c.current ? "is-current" : ""}`}>{Number(numOf[c.id]) || numOf[c.id]}</span>
              <ArrPaceDots scenes={c.scenes} />
              {c.time ? <span className="pace-time" title={c.time}>{c.time}</span> : null}
            </button>
          ))}
        </div>

        {/* POV swimlanes */}
        <div className="pace-lanes">
          <div className="pace-lanes-label">视角泳道</div>
          {povList.map((p) => (
            <div key={p} className="pace-row pace-lane" style={gridVars}>
              <div className="pace-lane-name" data-pov-tone={pov[p]} title={p}>{p}</div>
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

export { ArrPacingLens };
