import React from "react";
import { I } from "./icons.jsx";
import { Tag } from "./ws-ui.jsx";
import { RULE_DIMENSION_LABELS, findingLabel, findingSeverityLabel, findingSeverityTone } from "./labels/finding.js";

/* ==========================================================
   ws-finding-ui — 一条诊断发现怎么显示（文学质量的巡检条目、成稿中心的诊断页签共用）
   ----------------------------------------------------------
   发现是服务端的统一形状（literary_quality / scene_diagnosis：signal_id、label、dimension、severity、
   issue、recommendation、证据摘录、related、origin.carried_from、stale）。findingView 把它读成要显示的字，
   FindingLine 按两种版式画出来：
   · layout="card"（文学质量）：严重度标签 + 名字 +「在写作台看这一处」一行，下面问题 / 证据摘录 / 改法；
   · layout="row"（成稿中心的诊断页签）：左边一枚严重度色块写名字，中间问题 / 改法 / 与哪一段有关 / 沿用上次 /
     引文已不在正文，右边「在写作台看这一处」（只有钉得到原文的发现才有）。
   写作台深改抽屉的发现行更丰富（定位、忽略、AI 看这一处），另有自己的行，只和这里共用词表（labels/finding.js）。
   writerIntents：进写作台某一场（可带深改姿态与这条发现的 signal_id）的一组视图意图——章节编排、成稿中心、
   文学质量都用它去写作台。纯展示与纯函数：不读 store、不写 window。
   ========================================================== */

/* 证据摘录来自作者稿（HTML），截断处可能带半个标签：只留文字 */
export function findingPlainText(value) {
  return String(value || "")
    .replace(/^[a-z/]{1,6}>/i, "")
    .replace(/<[^>]*(>|$)/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/* 与哪一段有关（跨段检查给的 related）：「与第 3 段矛盾：……」 */
function relatedText(related) {
  if (!related) return "";
  const where = Number.isInteger(related.paragraph_index) ? `与第 ${related.paragraph_index + 1} 段` : "与另一段";
  return `${where}${related.label || "矛盾"}：${related.excerpt || ""}`;
}

/* 一条发现要显示的全部字。evidence：调用方另给的证据摘录（章组复审的条目把它放在 evidence_excerpt 之外）。 */
export function findingView(finding, { evidence } = {}) {
  const f = finding || {};
  return {
    label: findingLabel(f),
    /* 名字是英文键退回来的「其他维度」时，把原键放进悬停提示 */
    rawDimension: f.label || RULE_DIMENSION_LABELS[f.dimension] ? "" : String(f.dimension || ""),
    severity: f.severity || "",
    severityLabel: findingSeverityLabel(f.severity),
    severityTone: findingSeverityTone(f.severity),
    issue: String(f.issue || ""),
    fix: String(f.recommendation || f.recommended_action || ""),
    english: f.issue_en ? [f.issue_en, f.recommendation_en].filter(Boolean).join("\n") : "",
    excerpt: findingPlainText(f.context || evidence),
    signalId: f.signal_id || f.quality_signal_id || "",
    anchored: !!f.evidence,
    related: relatedText(f.related),
    carried: !!(f.origin && f.origin.carried_from),
    stale: !!f.stale,
  };
}

/* 进写作台这一场的视图意图。deep：带深改姿态；signalId：深改面板到了诊断就选中这一条（隐含 deep）。
   没有 sid（章级结果没有单一场景可去）时是空数组：只回写作台。 */
export function writerIntents(sid, { deep = false, signalId = "" } = {}) {
  if (!sid) return [];
  const intents = [{ type: "ws:writer-scene", detail: sid }];
  if (signalId) intents.push({ type: "ws:writer-posture", detail: { posture: "deep", signal_id: signalId } });
  else if (deep) intents.push({ type: "ws:writer-posture", detail: "deep" });
  return intents;
}

/* 一条发现。onLocate(signalId) 给了就有「在写作台看这一处」：card 版式只要有 signal_id，
   row 版式还要这条发现钉得到原文（章级判断没有落点）。 */
export function FindingLine({ finding, evidence, layout = "card", onLocate }) {
  const view = findingView(finding, { evidence });
  if (layout === "row") {
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
  return (
    <li className="q-finding">
      <div className="q-finding-head">
        <Tag tone={view.severityTone}>{view.severityLabel}</Tag>
        <strong title={view.rawDimension || undefined}>{view.label}</strong>
        {onLocate && view.signalId && (
          <button type="button" className="btn btn-quiet btn-sm q-finding-go" onClick={() => onLocate(view.signalId)}>
            <I.Pen size={12} /> 在写作台看这一处
          </button>
        )}
      </div>
      {view.issue && <p className="q-finding-issue" title={view.english || undefined}>{view.issue}</p>}
      {view.excerpt && <blockquote className="q-finding-evidence">{view.excerpt}</blockquote>}
      {view.fix && <p className="q-finding-fix">改法：{view.fix}</p>}
    </li>
  );
}
