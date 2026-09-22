import React from "react";
import { I } from "./icons.jsx";
import { LIB_chapterLabel, LIB_entrySub } from "./ws-library-derive.jsx";
import { LibEntryRow, LibGlyph, libAccClass } from "./ws-library-parts.jsx";
import { EmptyState, SectionLabel } from "./ws-ui.jsx";

const { useMemo: useTlMemo } = React;

/* ==========================================================
   Library — 叙事时间线
   两条轨道都只来自大事记本身：
   · 故事内时间：按作者写的时间排（能读出四位年份的按年份，其余保持作者的顺序，未定时间的排最后）；
   · 按章：大事记的「所在章」经目录解析成「第 N 章 · 标题」，按目录顺序排开。
   2026-09-21：删掉了原型写死的「现在 · 正文进行中 · CH01 – CH08」终点卡，
   以及从从未写入的 appears 字段推出的「章节脉络 / 贯穿全书」。
   键盘：事件卡和按章的条目都是按钮，空格选中、回车直接打开档案；选中后下方的条也能打开。
   ========================================================== */

const TL_HINT = "点一下选中，双击或按回车打开档案";

/* 回车打开档案（不再只靠双击）；空格照常走 click → 选中 */
const tlKeyOpen = (onOpen, id) => (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); onOpen(id); }
};

const tlYear = (label) => {
  const m = /(\d{4})/.exec(String(label || ""));
  return m ? parseInt(m[1], 10) : null;
};

function TlMinis({ entry, byId }) {
  const links = (entry.links || []).map(l => byId[l.id]).filter(Boolean).slice(0, 4);
  if (!links.length) return null;
  return (
    <span className="tl-event-glyphs">
      {links.map(t => <LibGlyph key={t.id} entry={t} size="xs" round title={t.name} />)}
    </span>
  );
}

function LibTimeline({ selId, onSelect, onOpen, onNew, entries, byId, chapters }) {
  const ents = entries || [];
  const bid = byId || {};
  const chapterList = chapters || [];

  /* 故事内时间：有时间的在前（其中读得出年份的按年份），未定时间的在后；同档保持作者的顺序 */
  const events = useTlMemo(() => {
    return ents
      .filter(e => e.cat === "events")
      .map((e, index) => ({ e, index, dated: !!String(e.timeLabel || "").trim(), year: tlYear(e.timeLabel) }))
      .sort((a, b) => {
        if (a.dated !== b.dated) return a.dated ? -1 : 1;
        const ya = a.year == null ? Infinity : a.year;
        const yb = b.year == null ? Infinity : b.year;
        if (ya !== yb) return ya - yb;
        return a.index - b.index;
      })
      .map(x => x.e);
  }, [ents]);

  /* 按章：目录顺序；找不到的章单独排在最后 */
  const byChapter = useTlMemo(() => {
    const order = new Map();
    chapterList.forEach((c, i) => { if (c && c.backendId) order.set(c.backendId, i); if (c && c.id) order.set(c.id, i); });
    const map = new Map();
    events.forEach(e => {
      const ref = String(e.chapterRef || "").trim();
      if (!ref) return;
      if (!map.has(ref)) map.set(ref, []);
      map.get(ref).push(e);
    });
    return Array.from(map.entries())
      .map(([ref, items]) => ({ ref, label: LIB_chapterLabel(ref, chapterList), rank: order.has(ref) ? order.get(ref) : Infinity, items }))
      .sort((a, b) => a.rank - b.rank);
  }, [events, chapterList]);

  const sel = selId ? bid[selId] : null;

  return (
    <div className="lib2-timeline">
      <section className="tl-section" aria-label="故事内时间">
        <SectionLabel icon="Clock">故事内时间</SectionLabel>
        {events.length === 0 ? (
          <EmptyState compact icon="Clock" title="还没有大事记"
            actions={onNew ? <button type="button" className="btn btn-ghost btn-sm" onClick={onNew}><I.Plus size={13} /> 新建档案</button> : null}>
            新建档案时选「大事记」并写上时间，它会按时间排进这条线。
          </EmptyState>
        ) : (
          <div className="tl-era">
            {events.map((e, i) => (
              <React.Fragment key={e.id}>
                <button
                  type="button"
                  className={`tl-event ${libAccClass(e.accent)} ${selId === e.id ? "is-sel" : ""}`}
                  aria-pressed={selId === e.id}
                  onClick={() => onSelect(e.id)}
                  onDoubleClick={() => onOpen(e.id)}
                  onKeyDown={tlKeyOpen(onOpen, e.id)}
                  title={TL_HINT}
                >
                  <span className={`tl-event-year ${e.timeLabel ? "" : "is-undated"}`}>{e.timeLabel || "未定时间"}</span>
                  <span className="tl-event-name">{e.name}</span>
                  {e.chapterRef && <span className="tl-event-sum">{LIB_chapterLabel(e.chapterRef, chapterList)}</span>}
                  <TlMinis entry={e} byId={bid} />
                </button>
                {i < events.length - 1 && <span className="tl-arrow" aria-hidden="true"><I.ChevronRight size={20} /></span>}
              </React.Fragment>
            ))}
          </div>
        )}
      </section>

      <section className="tl-section" aria-label="按章">
        <SectionLabel icon="BookOpen">按章</SectionLabel>
        {byChapter.length === 0 ? (
          <p className="lib-quiet">
            {events.length
              ? "大事记还没有标「所在章」。编辑一条大事记，选它发生在哪一章，这里就会按章排开。"
              : "有了大事记并标上「所在章」，这里会按章排开。"}
          </p>
        ) : (
          <div className="tl-chaps">
            {byChapter.map(({ ref, label, items }) => (
              <div className="tl-chap" key={ref}>
                <div className="tl-chap-head">
                  <span className="tl-chap-no">{label}</span>
                  <span className="tl-chap-n">{items.length}</span>
                </div>
                <div className="tl-chap-list">
                  {items.map(e => (
                    <LibEntryRow
                      key={e.id}
                      entry={e}
                      variant="card"
                      glyphSize="xs"
                      active={selId === e.id}
                      className="tl-chip"
                      aria-pressed={selId === e.id}
                      onClick={() => onSelect(e.id)}
                      onDoubleClick={() => onOpen(e.id)}
                      onKeyDown={tlKeyOpen(onOpen, e.id)}
                      title={TL_HINT}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {sel && (
        <div className={`tl-selbar ${libAccClass(sel.accent)}`} role="status">
          <LibEntryRow entry={sel} glyphSize="sm" sub={LIB_entrySub(sel)} />
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => onOpen(sel.id)}><I.BookOpen size={13} /> 打开档案</button>
        </div>
      )}
    </div>
  );
}

export { LibTimeline };
