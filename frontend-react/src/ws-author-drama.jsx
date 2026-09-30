import React from "react";
import { I } from "./icons.jsx";
import { DRAMA_KEYS } from "./ws-author-derive.js";
import { Tag } from "./ws-ui.jsx";

/* ==========================================================
   章节详情 · 戏剧卡（从 ws-author-detail.jsx 拆出，2026-10）
   这一章的写法：承诺、推进和余味，外加两条护栏（禁止包含 / 备注）。它归章节编排（不在构思里），
   会进章节蓝图和本章每一场的 AI 起草上下文；留空也能写。一格都没写时收成一行。
   ========================================================== */

const { useEffect, useState } = React;

const ARR_DRAMA_GROUPS = [
  { key: "promise", label: "承诺", icon: "Star", fields: [
    { k: "promise", label: "核心承诺", hint: "读完这一章读者会得到什么", primary: true },
    { k: "problem", label: "章节问题", hint: "本章想问读者一个什么问题" },
  ] },
  { key: "drive", label: "推进", icon: "ArrowRight", fields: [
    { k: "spine", label: "主线推进", hint: "本章在全书主线上前进了多少" },
    { k: "arc", label: "人物变化", hint: "主要人物的内在或外在变化" },
  ] },
  { key: "close", label: "收束", icon: "Sparkles", fields: [
    { k: "aftertaste", label: "结尾余味", hint: "读完最后一段的感觉" },
    { k: "ending", label: "结尾效果", hint: "最后一句具体的画面 / 动作" },
  ] },
];

/* 戏剧卡的一格。textarea 不受控（边写边存会打断输入法），所以 key 带上服务端的值：
   AI 编排写入、目录重拉带来新值时换一个新的框显示新值；失焦时只有真的改了才写回——
   以前 key 只有章 id，重拉之后框里还是旧字，下一次失焦把旧字写回去，把刚应用的建议悄悄冲掉。 */
function ArrDramaText({ className, label, value, ck, locked, onCommit, placeholder }) {
  const v = value || "";
  return (
    <textarea className={className} defaultValue={v} key={`${ck}|${v}`} placeholder={placeholder} disabled={locked}
      aria-label={label}
      onBlur={(e) => { if (e.target.value !== v && onCommit) onCommit(e.target.value); }} />
  );
}

function ArrDramaCard({ ch, locked, onPatchDrama, sectionRef }) {
  const drama = ch.drama || {};
  const filled = DRAMA_KEYS.filter((k) => String(drama[k] || "").trim()).length;
  const guarded = !!(String(drama.forbidden || "").trim() || String(drama.notes || "").trim());
  /* null = 跟着内容走（一格都没写就收成一行）；作者点过就听作者的。偏好记着是哪一章的，换章即回到「跟着内容走」。
     自动展开只往「开」的方向走：开过一次就钉住——清空唯一写过的一格、Tab 到下一格时，失焦写回让内容变空，
     卡片要是跟着收起，刚拿到焦点的那一格就被卸掉，焦点掉到 body 上。 */
  const autoOpen = filled > 0 || guarded;
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
          <span className="card-title" id={`${bodyId}-title`}>戏剧卡</span>
          <Tag tone="neutral" className="tab-num">可选 · {filled}/{DRAMA_KEYS.length}</Tag>
          <I.ChevronDown size={15} className="arr-drama-chev" />
        </span>
        <span className="card-sub">这一章的写法：承诺、推进和余味。它归这里（不在构思里），会进章节蓝图和本章每一场的 AI 起草上下文；留空也能写。</span>
      </button>
      {open && (
        <div className="arr-drama-groups" id={bodyId}>
          {ARR_DRAMA_GROUPS.map((g) => {
            const Ic = I[g.icon] || I.Dot;
            return (
              <div className="arr-dgroup" key={g.key}>
                <header className="arr-dgroup-head"><Ic size={13} /><span>{g.label}</span><i className="arr-dgroup-rule" /></header>
                <div className="arr-dgroup-fields">
                  {g.fields.map((f) => (
                    <div className={`arr-field ${f.primary ? "is-primary" : ""}`} key={f.k}>
                      <header className="arr-field-head">
                        <span className="arr-field-label">{f.label}</span>
                        <span className="arr-field-hint">{f.hint}</span>
                      </header>
                      <ArrDramaText className="arr-field-text" label={f.label} value={drama[f.k]} ck={`${ch.id}|${f.k}`}
                        locked={locked} placeholder="还没写" onCommit={(v) => onPatchDrama(f.k, v)} />
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
          <div className="arr-dgroup arr-dgroup-guard">
            <header className="arr-dgroup-head"><I.ShieldCheck size={13} /><span>护栏</span><i className="arr-dgroup-rule" /></header>
            <div className="arr-guard-grid">
              <div className="arr-guard" data-tone="danger">
                <div className="arr-guard-label"><I.Ban size={12} /> 禁止包含</div>
                <ArrDramaText className="arr-guard-text" label="禁止包含" value={drama.forbidden} ck={`${ch.id}|forbidden`} locked={locked}
                  placeholder="这一章不能出现的词或情节，一行一条" onCommit={(v) => onPatchDrama("forbidden", v)} />
              </div>
              <div className="arr-guard" data-tone="info">
                <div className="arr-guard-label"><I.Quote size={12} /> 备注</div>
                <ArrDramaText className="arr-guard-text" label="章节备注" value={drama.notes} ck={`${ch.id}|notes`} locked={locked}
                  placeholder="给自己留的话" onCommit={(v) => onPatchDrama("notes", v)} />
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

export { ArrDramaCard };
