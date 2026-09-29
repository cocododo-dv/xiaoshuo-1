import React from "react";
import { I } from "./icons.jsx";
import { S2AutoText } from "./ws-snow-fields.jsx";
import { s2BusyOn } from "./ws-snow-model.js";

/* ==========================================================
   雪花编辑器的共用小件（2026-09-29 从 ws-snow-scaffolds.jsx 拆出）
   ----------------------------------------------------------
   04 / 06 / 08 的角色页签与「AI 补全此角色」按钮，05 / 07 的「展开自上一层」一行——以前各抄一遍。
   ========================================================== */

/* 角色页签：tabs = [{ id, name, sub（名字下面那一行）, tone?（按定位着色）, legacy?（不在 04 名册里的历史角色）}] */
export function S2CharTabs({ tabs, sel, onSelect, onAdd, addTitle }) {
  return (
    <div className="sf-char-tabs">
      {tabs.map(t => (
        <button key={t.id} className={`sf-char-tab ${t.tone ? `tone-${t.tone}` : ""} ${sel === t.id ? "is-sel" : ""}`} onClick={() => onSelect(t.id)}>
          <span className="sf-char-av text-serif">{(t.name || "?")[0]}</span>
          <span className="sf-char-tab-body">
            <span className="sf-char-tab-name">{t.name || "未命名"}{t.legacy && <em className="sf-char-legacy" title="这个角色不在 04 名册里（历史数据）">·遗留</em>}</span>
            <span className="sf-char-tab-role">{t.sub}</span>
          </span>
        </button>
      ))}
      <button className="sf-char-add" onClick={onAdd} title={addTitle}><I.Plus size={15} /></button>
    </div>
  );
}

/* 「AI 补全此角色」：只补全当前选中的这个角色，其余角色不动；没有 AI 工具面时不画 */
export function S2CharFillButton({ ai, id, name }) {
  if (!ai) return null;
  const busy = s2BusyOn(ai, "fill_char", id);
  return (
    <button className="btn btn-quiet btn-sm" disabled={ai.structBusy} onClick={() => ai.onFillChar(id, name)}
      title="只让 AI 补全当前选中的这个角色——其余角色保持不动（依据上游材料，与其他角色保持一致）">
      <I.Wand size={13} className={busy ? "sf-spin" : ""} /> {busy ? "生成中…" : "AI 补全此角色"}
    </button>
  );
}

/* 分形展开的一行：左边是节拍（序号 · 名字 · 说明），右边是它展开自的上一层原文、这一层的文本框、长了没有 */
export function S2ExpansionRow({ beat, index, src, srcTag, srcTitle, emptyText, value, onChange, minRows, ariaLabel, placeholder, meta, grew }) {
  return (
    <div className={`sf-syn-row tone-${beat.tone}`}>
      <div className="sf-syn-side">
        <span className="sf-syn-idx">{index + 1}</span>
        <span className="sf-syn-label">{beat.label}</span>
        <span className="sf-syn-desc">{beat.desc}</span>
      </div>
      <div className="sf-syn-main">
        <div className="sf-syn-src" title={srcTitle}>
          <span className="sf-syn-src-tag">{srcTag}</span>
          <span className="sf-syn-src-text">{src || <em className="sf-syn-empty">{emptyText}</em>}</span>
        </div>
        <S2AutoText className="sf-syn-text" minRows={minRows} value={value} onChange={(e) => onChange(e.target.value)} aria-label={ariaLabel} placeholder={placeholder} />
        {value.trim() && <div className={`sf-syn-meta ${grew ? "is-ok" : "is-warn"}`}>{meta}</div>}
      </div>
    </div>
  );
}
