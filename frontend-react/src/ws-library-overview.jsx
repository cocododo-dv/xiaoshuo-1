import React from "react";
import { I } from "./icons.jsx";
import { agoLabel } from "./lib/ago.js";
import { LIB_entrySub } from "./ws-library-derive.jsx";
import { LibEntryRow, libAccClass, libCatLabel } from "./ws-library-parts.jsx";
import { SectionLabel } from "./ws-ui.jsx";

/* ==========================================================
   Library — 故事圣经总览（进入资料库的落地页）
   只说数据里真有的事：各类有几份、哪些还没写简述、哪些还没和别的档案连起来、最近改过哪些、置顶了哪些。
   原型的「就绪度 100%」「待你处理」队列删掉了——条目根本没有这些状态。
   平铺在详情面板里（区块之间用分隔线），不再卡片套卡片。
   ========================================================== */

const OV_LIST_MAX = 6;

function OvEntryRow({ e, sub, onSelect }) {
  return <LibEntryRow entry={e} sub={sub} glyphSize="sm" chevron onClick={() => onSelect(e.id)} />;
}

function OvMore({ n }) {
  return n > 0 ? <div className="ov-more">还有 {n} 份</div> : null;
}

function LibOverview({ facts, onSelect, onPickCat, onGoGraph }) {
  const { total, linksN, byCat, missingBlurb, isolated, recent, pinned } = facts;

  return (
    <div className="ov" data-screen-label="library-overview">
      <header className="ov-head">
        <div className="ov-head-main">
          <h2 className="ov-title">总览</h2>
          <p className="ov-sub">{total} 份档案，{linksN} 条关联。</p>
        </div>
        {linksN > 0 && (
          <div className="ov-head-actions">
            <button type="button" className="btn btn-ghost btn-sm" onClick={onGoGraph}><I.Compass size={13} /> 看关系图谱</button>
          </div>
        )}
      </header>

      <div className="ov-cats" role="group" aria-label="按类别查看">
        {byCat.map(({ cat, n }) => {
          const Ic = I[cat.icon] || I.Dot;
          return (
            <button type="button" key={cat.id} className={`ov-cat ${libAccClass(cat.accent)}`} onClick={() => onPickCat(cat.id)}>
              <span className="ov-cat-ic" aria-hidden="true"><Ic size={15} /></span>
              <span className="ov-cat-n">{n}</span>
              <span className="ov-cat-label">{cat.label}</span>
            </button>
          );
        })}
      </div>

      <div className="ov-grid">
        <section className="ov-section">
          <SectionLabel icon="Edit" aside={missingBlurb.length || null}>还没写简述</SectionLabel>
          {missingBlurb.length === 0 ? (
            <p className="lib-quiet">每份档案都写了简述。</p>
          ) : (
            <div className="lib-rows">
              {missingBlurb.slice(0, OV_LIST_MAX).map(e => (
                <OvEntryRow key={e.id} e={e} sub={libCatLabel(e.cat)} onSelect={onSelect} />
              ))}
              <OvMore n={missingBlurb.length - OV_LIST_MAX} />
            </div>
          )}
        </section>

        <section className="ov-section">
          <SectionLabel icon="Clock">最近改动</SectionLabel>
          {recent.length === 0 ? (
            <p className="lib-quiet">还没有改动记录。</p>
          ) : (
            <div className="lib-rows">
              {recent.map(e => (
                <OvEntryRow key={e.id} e={e} sub={agoLabel(e.updatedAt)} onSelect={onSelect} />
              ))}
            </div>
          )}
        </section>

        <section className="ov-section">
          <SectionLabel icon="Link" aside={isolated.length || null}>还没有任何关联</SectionLabel>
          {isolated.length === 0 ? (
            <p className="lib-quiet">每份档案都至少和另一份连着。</p>
          ) : (
            <>
              <p className="lib-quiet">给它们加上关系，关系图谱里才会把它们连起来。</p>
              <div className="ov-chips">
                {isolated.map(e => (
                  <LibEntryRow key={e.id} entry={e} variant="chip" glyphSize="xs" onClick={() => onSelect(e.id)} />
                ))}
              </div>
            </>
          )}
        </section>

        <section className="ov-section">
          <SectionLabel icon="Star" aside={pinned.length || null}>置顶</SectionLabel>
          {pinned.length === 0 ? (
            <p className="lib-quiet">在档案里点「置顶」，常用的几份会排在最前。</p>
          ) : (
            <div className="lib-rows">
              {pinned.map(e => (
                <OvEntryRow key={e.id} e={e} sub={LIB_entrySub(e)} onSelect={onSelect} />
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

export { LibOverview };
