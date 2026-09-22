import React from "react";
import { I } from "./icons.jsx";
import { Tag } from "./ws-ui.jsx";
import { SCENE_STATE_META } from "./ws-labels.js";

/* ==========================================================
   主页底部「全书 N 章」：进度脊（每章一段）+ 前线章附近的几张章卡。
   模型全在 ws-home-derive.js（hmDeriveSpine / hmChapterWindow）。章的阶段与叫法和成稿中心
   同一份（ws-labels）：规划中但已经有字的章，这里和成稿中心都读作「写作中」；场的三态也是同一份词。
   ========================================================== */

/* diagnosis：{ byChapter: { [目录章 id]: { open, blocking } }, totals: { open } | null }——写作台深改面板里还开着的发现数 */
function HmChapters({ spine, windowed, allChapters, openScene, diagnosis = null }) {
  const byChapter = (diagnosis && diagnosis.byChapter) || {};
  const totals = (diagnosis && diagnosis.totals) || null;
  return (
    <section className="hm-chaps" aria-label="全书章节">
      <div className="hm-chaps-head">
        <h2 className="hm-chaps-title">全书 {spine.total} 章</h2>
        <button type="button" className="btn btn-quiet btn-sm" title={allChapters.title} onClick={allChapters.onClick}>
          {allChapters.label} <I.ArrowRight size={13} />
        </button>
      </div>
      <div className="hm-spine" data-testid="home-spine">
        <div className="hm-spine-head">
          <div className="hm-spine-legend" aria-label="各阶段章数">
            {spine.legend.map(l => (
              <span key={l.state} className={`hm-leg st-${l.state}`}><i />{l.label} <b>{l.n}</b></span>
            ))}
          </div>
          <div className="hm-spine-scenes">
            {spine.scenes.total ? (
              <>
                <span>已规划 <b>{spine.scenes.total}</b> 场</span>
                <span>{SCENE_STATE_META.done.label} {spine.scenes.done}</span>
                <span>{SCENE_STATE_META.writing.label} {spine.scenes.writing}</span>
                <span>{SCENE_STATE_META.todo.label} {spine.scenes.todo}</span>
                {totals && totals.open > 0 && (
                  <span className="hm-spine-diag" title="写作台深改面板里还开着的诊断发现（忽略过的不算）">诊断待改 <b>{totals.open}</b></span>
                )}
              </>
            ) : "还没有规划场景"}
          </div>
        </div>
        <div className="hm-spine-bar" aria-label="全书各章所在阶段">
          {spine.segments.map(s => (
            <button
              key={s.id || s.n} type="button"
              className={`hm-seg s-${s.state} ${s.front ? "is-front" : ""}`}
              data-testid={`home-spine-ch-${s.n}`}
              title={s.hint} aria-label={s.hint}
              onClick={() => openScene(s.sid)}
            >
              {s.front && <span className="hm-seg-flag">前线</span>}
            </button>
          ))}
        </div>
      </div>
      <div className="hm-chaps-sub">{windowed.partial ? "当前章附近" : "各章"}</div>
      <div className="hm-chap-track">
        {windowed.cards.map(c => (
          <button key={c.id || c.n} type="button" className={`hm-chap s-${c.state} ${c.front ? "is-active" : ""}`}
            aria-current={c.front ? "true" : undefined} onClick={() => openScene(c.sid)}>
            <div className="hm-chap-top">
              {c.title ? <span className="hm-chap-n">{c.num}</span> : <span />}
              <span className="hm-chap-tags">
                {byChapter[c.id] && byChapter[c.id].open > 0 && (
                  <span className="hm-chap-diag" data-testid={`home-chap-diag-${c.n}`} title="这一章各场还开着的诊断发现">诊断 {byChapter[c.id].open}</span>
                )}
                <Tag tone={c.tone}>{c.stateLabel}</Tag>
              </span>
            </div>
            <div className="hm-chap-t">{c.title || c.num}</div>
            <div className="hm-chap-bar" aria-hidden="true"><i style={{ width: c.pct + "%" }} /></div>
          </button>
        ))}
      </div>
    </section>
  );
}

export { HmChapters };
