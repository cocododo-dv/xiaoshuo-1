import React from "react";
import { findingLabel, findingSeverityLabel } from "./labels/finding.js";

/* ==========================================================
   ws-finding-ui — 一条诊断发现在成稿中心「通读」页签里怎么显示（整章的判断、落到各场的通读发现）
   ----------------------------------------------------------
   发现是服务端的统一形状（scene_diagnosis：signal_id、label、dimension、severity、issue、recommendation、证据摘录、
   related、origin.carried_from、stale）。findingView 把它读成要显示的字；FindingLine 画一行：左边一枚严重度色块写
   名字，中间问题 / 改法 / 与哪一段有关 / 沿用上次 / 引文已不在正文，右边「在写作台看这一处」（只有钉得到原文的
   发现才有）。文学质量页的卡片版式随那一页于 2026-10-20 退役。
   写作台深改抽屉的发现行更丰富（定位、忽略、AI 看这一处），另有自己的行，只和这里共用词表（labels/finding.js）。
   去写作台看这一处的视图意图由 ws-view-intents.js 的 writerIntents 给。纯展示与纯函数：不读 store、不写 window。
   ========================================================== */

/* 与哪一段有关（跨段检查给的 related）：「与第 3 段矛盾：……」 */
function relatedText(related) {
  if (!related) return "";
  const where = Number.isInteger(related.paragraph_index) ? `与第 ${related.paragraph_index + 1} 段` : "与另一段";
  return `${where}${related.label || "矛盾"}：${related.excerpt || ""}`;
}

/* 一条发现要显示的全部字 */
export function findingView(finding) {
  const f = finding || {};
  return {
    label: findingLabel(f),
    severity: f.severity || "",
    severityLabel: findingSeverityLabel(f.severity),
    issue: String(f.issue || ""),
    fix: String(f.recommendation || f.recommended_action || ""),
    signalId: f.signal_id || f.quality_signal_id || "",
    anchored: !!f.evidence,
    related: relatedText(f.related),
    carried: !!(f.origin && f.origin.carried_from),
    stale: !!f.stale,
  };
}

/* 一条发现。onLocate(signalId) 给了、且这条发现钉得到原文，就有「在写作台看这一处」（章级判断没有落点）。 */
export function FindingLine({ finding, onLocate }) {
  const view = findingView(finding);
  return (
    <li className="ms-diag-row">
      <span className={`ms-diag-mark sev-${view.severity}`} title={`严重程度：${view.severityLabel}`}>{view.label}</span>
      <span className="ms-diag-body">
        <span className="ms-diag-t">{view.issue}</span>
        {view.fix && <span className="ms-diag-fix">改法：{view.fix}</span>}
        {view.related && <span className="ms-diag-fix">{view.related}</span>}
        {view.carried && <span className="ms-diag-carried">沿用上次通读</span>}
        {view.stale && <span className="ms-diag-stale">引的那句已经不在正文里</span>}
      </span>
      {onLocate && view.anchored && (
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => onLocate(view.signalId)}>在写作台看这一处</button>
      )}
    </li>
  );
}
