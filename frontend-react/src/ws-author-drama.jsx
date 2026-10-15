import React from "react";
import { I } from "./icons.jsx";
import { Tag } from "./ws-ui.jsx";
import { DRAMA_FIELDS, DRAMA_KEYS } from "./labels/catalog.js";
import { arrIsPlanChapter } from "./ws-author-derive.js";

/* ==========================================================
   章节详情 · 章的说明（原戏剧卡；从 ws-author-detail.jsx 拆出，2026-10）
   三格：这一章给读者什么 / 先别揭 / 备注（后端 narrative_json.drama 的 promise / forbidden / notes，services/chapter_intent.py）。
   2026-10-16 规划层收拢：原戏剧卡的另外五格都是这一章几场三拍的再概括，退役；章的说明也只在一处写——雪花整理出来的章
   在构思的分章面板里写（这里只读，旁边一扇门），手建的章在这里写。
   前两格进章节蓝图（章节蓝图卡读的、起草前现做的都读；改了它们，蓝图会提示过时）；「先别揭」一行一条，还进起草提示里
   的场景卡，整行写进正文会按字面查出来（起草验收、运行前预检、质检复核、成稿门，后端 qc_constraints）。备注只给作者看。
   ========================================================== */

const { useEffect, useState } = React;

/* 一格。textarea 不受控（边写边存会打断输入法），所以 key 带上服务端的值：
   目录重拉带来新值时换一个新的框显示新值；失焦时只有真的改了才写回——
   以前 key 只有章 id，重拉之后框里还是旧字，下一次失焦把旧字写回去，把服务端的新值悄悄冲掉。 */
function ArrDramaText({ className, label, value, ck, locked, onCommit, placeholder }) {
  const v = value || "";
  return (
    <textarea className={className} defaultValue={v} key={`${ck}|${v}`} placeholder={placeholder} disabled={locked}
      aria-label={label}
      onBlur={(e) => { if (e.target.value !== v && onCommit) onCommit(e.target.value); }} />
  );
}

function ArrDramaCard({ ch, locked, onPatchDrama, onOpenPlan, sectionRef }) {
  const drama = ch.drama || {};
  const filled = DRAMA_KEYS.filter((k) => String(drama[k] || "").trim()).length;
  /* 雪花整理出来的章：章的说明在构思的分章面板里写，这里只读 */
  const fromPlan = arrIsPlanChapter(ch);
  /* null = 跟着内容走（一格都没写就收成一行）；作者点过就听作者的。偏好记着是哪一章的，换章即回到「跟着内容走」。
     自动展开只往「开」的方向走：开过一次就钉住——清空唯一写过的一格、Tab 到下一格时，失焦写回让内容变空，
     卡片要是跟着收起，刚拿到焦点的那一格就被卸掉，焦点掉到 body 上。 */
  const autoOpen = filled > 0;
  const [pref, setPref] = useState(() => ({ id: ch.id, open: null }));
  const openPref = pref.id === ch.id ? pref.open : null;
  useEffect(() => {
    if (openPref == null && autoOpen) setPref({ id: ch.id, open: true });
  }, [ch.id, openPref, autoOpen]);
  const open = openPref == null ? autoOpen : openPref;
  const setOpenPref = (value) => setPref({ id: ch.id, open: value });
  const bodyId = `arr-drama-${ch.id}`;
  return (
    <section className={`card arr-drama ${open ? "is-open" : "is-collapsed"}`} ref={sectionRef} aria-labelledby={`${bodyId}-title`}>
      <button type="button" className="arr-drama-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpenPref(!open)}>
        <span className="arr-drama-toggle-main">
          <span className="card-title" id={`${bodyId}-title`}>章的说明</span>
          <Tag tone="neutral" className="tab-num">可选 · {filled}/{DRAMA_KEYS.length}</Tag>
          <I.ChevronDown size={15} className="arr-drama-chev" />
        </span>
        <span className="card-sub">
          这一章给读者什么、先别揭什么。前两格进章节蓝图；「先别揭」起草时告诉 AI，写进正文会被按字面查出来；备注只给你看。留空也能写。
        </span>
      </button>
      {open && (
        <div className="arr-drama-groups" id={bodyId}>
          {fromPlan && (
            <p className="arr-drama-door">
              <span>这一章是构思里分出来的：章的说明在分章面板里写，这里只读。</span>
              {onOpenPlan && (
                <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenPlan}>
                  <I.Layout size={13} /> 在分章面板里改
                </button>
              )}
            </p>
          )}
          <div className="arr-dgroup-fields">
            {DRAMA_FIELDS.map((f) => (
              <div className={`arr-field ${f.key === "promise" ? "is-primary" : ""}`} key={f.key}>
                <header className="arr-field-head">
                  <span className="arr-field-label">{f.label}</span>
                  <span className="arr-field-hint">{f.hint}</span>
                </header>
                <ArrDramaText className="arr-field-text" label={f.label} value={drama[f.key]} ck={`${ch.id}|${f.key}`}
                  locked={locked || fromPlan} placeholder={fromPlan ? "还没写" : f.placeholder}
                  onCommit={(v) => onPatchDrama(f.key, v)} />
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

export { ArrDramaCard };
