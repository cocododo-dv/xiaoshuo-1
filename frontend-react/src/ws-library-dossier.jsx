import React from "react";
import { I } from "./icons.jsx";
import { LIB_chapterLabel, LIB_groupConnections, LIB_relLabel } from "./ws-library-derive.jsx";
import { LIB_CAT_BY_ID, LibEntryRow, LibGlyph, libAccClass, libCatLabel } from "./ws-library-parts.jsx";
import { navigateWithViewIntent } from "./ws-view-intents.js";
import { IconButton, SectionLabel, Tag } from "./ws-ui.jsx";

/* ==========================================================
   资料 · 一份档案的阅读视图（Dossier）与它上面的导航条（DossierNav）。
   编辑 / 新建表单在 ws-library-edit.jsx（那里还挂着写作台用的 window 契约）。
   ========================================================== */

/* 导航条：回总览 / 当前类别，以及沿当前可见列表翻上一条、下一条 */
function DossierNav({ entry, pos, total, prev, next, onHome, onCat, onOpen }) {
  const prevLabel = prev ? "上一条：" + prev.name : "已是第一条";
  const nextLabel = next ? "下一条：" + next.name : "已是最后一条";
  return (
    <nav className="dossier-nav" aria-label="档案导航">
      <button type="button" className="dossier-nav-link is-home" onClick={onHome}><I.Activity size={13} aria-hidden="true" /> 总览</button>
      <span className="dossier-nav-sep" aria-hidden="true">/</span>
      <button type="button" className="dossier-nav-link" onClick={onCat}>{libCatLabel(entry.cat)}</button>
      <span className="dossier-nav-spacer" />
      <IconButton icon="ChevronLeft" label={prevLabel} variant="ghost" disabled={!prev} onClick={() => prev && onOpen(prev.id)} />
      {pos >= 0 && <span className="dossier-nav-pos">{pos + 1} / {total}</span>}
      <IconButton icon="ChevronRight" label={nextLabel} variant="ghost" disabled={!next} onClick={() => next && onOpen(next.id)} />
    </nav>
  );
}

function Dossier({ entry: e, conns, byId, chapters, onNav, onEdit, onDelete, onTogglePin }) {
  const meta = LIB_CAT_BY_ID[e.cat] || {};
  const Ic = I[meta.icon] || I.Dot;
  const cs = conns || [];
  const relGroups = LIB_groupConnections(cs);
  const isEvent = e.cat === "events";
  /* 说明行：人物写角色定位，世界写类型；大事记的时间放进下面的信息格 */
  const kindLine = e.cat === "people" ? e.role : e.cat === "world" ? e.kind : "";
  const factRows = isEvent
    ? [
        { k: "时间", v: e.timeLabel || "未定时间" },
        { k: "所在章", v: LIB_chapterLabel(e.chapterRef, chapters) || "未关联章节" },
      ]
    : (e.facts || []).filter(f => f && (f.k || f.v));
  /* 自定义编号只有和类别名不同时才显示，否则就是重复 */
  const showCode = e.code && e.code !== meta.label;
  return (
    <div className={`dossier ${libAccClass(e.accent)}`}>
      <header className="dossier-head">
        <LibGlyph entry={e} size="xl" />
        <div className="dossier-head-main">
          {showCode && <div className="dossier-code">{e.code}</div>}
          <h2 className="dossier-name">{e.name}</h2>
          {kindLine && <div className="dossier-kind"><Ic size={13} aria-hidden="true" />{kindLine}</div>}
          {e.summary && <p className="dossier-summary">{e.summary}</p>}
        </div>
        {!isEvent && (
          <div className="dossier-head-actions">
            <button
              type="button"
              className={`dossier-pin ${e.pinned ? "is-on" : ""}`}
              aria-pressed={!!e.pinned}
              onClick={onTogglePin}
              title={e.pinned ? "取消置顶" : "置顶：在列表和总览里排在最前"}
            >
              <I.Star size={14} /> {e.pinned ? "已置顶" : "置顶"}
            </button>
          </div>
        )}
      </header>

      <div className="dossier-body">
        {e.blurb ? (
          <section className="dossier-section">
            <SectionLabel>{isEvent ? "经过" : "简述"}</SectionLabel>
            <p className="dossier-blurb">{e.blurb}</p>
          </section>
        ) : (
          <button type="button" className="dossier-newhint" onClick={onEdit}>
            <I.Edit size={14} /> 这份档案还没有{isEvent ? "写经过" : "简述"}。点这里补上{isEvent ? "时间、所在章和经过" : "简述、关键信息与关联"}。
          </button>
        )}

        {factRows.length > 0 && (
          <section className="dossier-section">
            <SectionLabel>{isEvent ? "时间与位置" : "关键信息"}</SectionLabel>
            <dl className="dossier-facts">
              {factRows.map((f, i) => (
                <div key={i} className="dossier-fact"><dt className="k">{f.k || "未命名"}</dt><dd className="v">{f.v || "—"}</dd></div>
              ))}
            </dl>
          </section>
        )}

        {e.tags && e.tags.length > 0 && (
          <section className="dossier-section">
            <SectionLabel>标签</SectionLabel>
            <div className="dossier-tags">
              {e.tags.map(t => <Tag key={t}>{t}</Tag>)}
            </div>
          </section>
        )}

        {cs.length > 0 && (
          <section className="dossier-section">
            <SectionLabel aside={cs.length}>关系网络</SectionLabel>
            <div className="rel-groups">
              {relGroups.map(g => {
                const Ti = I[g.type.icon] || I.Dot;
                return (
                  <div key={g.type.id} className={`rel-group acc-${g.type.accent}`}>
                    <div className="rel-group-h">
                      <span className="rel-group-ic" aria-hidden="true"><Ti size={12} /></span>
                      <span className="rel-group-label">{g.type.label}</span>
                      <span className="rel-group-hint">{g.type.hint}</span>
                      <span className="rel-group-n">{g.items.length}</span>
                    </div>
                    <div className="lib-rows">
                      {g.items.map((l, i) => {
                        const t = byId[l.id];
                        if (!t) return null;
                        return (
                          <LibEntryRow
                            key={i}
                            entry={t}
                            variant="card"
                            glyphSize="sm"
                            chevron
                            onClick={() => onNav(l.id)}
                            sub={(
                              <>
                                {l.dir === "in" && <span className="lib-row-flag" title="由对方那一头建立的关联">对方关联</span>}
                                {LIB_relLabel(l)}
                              </>
                            )}
                            aside={<span className="lib-row-aside">{libCatLabel(t.cat)}</span>}
                          />
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}
      </div>

      <footer className="dossier-foot">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onEdit}><I.Edit size={13} /> 编辑档案</button>
        <button type="button" className="btn btn-quiet btn-sm"
          onClick={() => navigateWithViewIntent("writer", "ws:writer-locate", e.id)}
          title="到写作台里找这个名字出现的地方；当前这一场没有时，会换到正在写的那一场再找">
          <I.Pen size={13} /> 在正文中查找
        </button>
        <span className="spacer" />
        <button type="button" className="btn btn-danger btn-sm" onClick={onDelete}><I.Trash size={13} /> 删除</button>
      </footer>
    </div>
  );
}

export { Dossier, DossierNav };
