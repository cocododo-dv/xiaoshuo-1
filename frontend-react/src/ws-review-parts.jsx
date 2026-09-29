import React from "react";
import { I } from "./icons.jsx";
import { Tag, EmptyState } from "./ws-ui.jsx";
import { RV_KINDS } from "./ws-review-store.js";

/* 待办收件箱的展示件（2026-09-29 从 ws-review.jsx 拆出）：一张卡、优先级段头、清空时的空态，
   以及「这张卡能不能直接划掉」「没动作的卡补哪些动作」两条规则。 */

/* RV_KINDS 的色板名（旧契约，主页也读）→ ws-ui 的语气 */
export const RV_TONE = { crimson: "accent", rose: "danger", slate: "info", gold: "warn", sage: "ok" };

/* priority: 1 = 优先处理 · 2/3 = 其余。提示只说轻重，不替卡片的类型下结论。 */
export const RV_BAND = { 1: { label: "优先处理", hint: "尽快处理" }, 2: { label: "其余待办", hint: "不急，得空再看" } };

/* 决策类待办（带真实效果或候选项）与实时派生项不允许被「无决策地划掉」：
   派生项只能去源头处理（修好自动消失）或稍后；快捷键 E / 全部处理完遇到它们改为展开 */
export const rvNeedsChoice = (it) => !!(it && (it.live || (it.actions || []).some(a => a.effect) || it.options));

/* 没有动作的卡（旧表行）也要能用鼠标处理掉：补「知道了 / 稍后」。实时派生卡不能划掉，只给「稍后」。 */
export function rvActionsOf(item) {
  if (item.actions && item.actions.length) return item.actions;
  const snooze = { label: "稍后", intent: "quiet", op: "snooze", fallback: true };
  return item.live ? [snooze] : [{ label: "知道了", intent: "ghost", op: "resolve", fallback: true }, snooze];
}

export function RvBand({ band }) {
  const b = RV_BAND[band];
  return (
    <div className={`rv-band b-${band}`}>
      <span className="rv-band-label">{b.label}</span>
      <span className="rv-band-hint">{b.hint}</span>
      <span className="rv-band-rule" />
    </div>
  );
}

export function RvItem({ item, open, removing, selected, onToggle, onAct }) {
  const m = RV_KINDS[item.kind] || RV_KINDS.note;
  const Ic = I[m.icon] || I.Dot;
  const blocking = item.qualityLevel === "Q0" || item.qualityLevel === "Q1";
  const actions = rvActionsOf(item);
  return (
    <article role="listitem" data-id={item.id} className={`rv-item t-${m.tone} ${open ? "is-open" : ""} ${removing ? "is-removing" : ""} ${selected ? "is-sel" : ""} ${item.priority === 1 ? "is-hot" : ""}`}>
      <span className="rv-spine" aria-hidden="true" />
      <div className="rv-body">
        <button type="button" className="rv-row" onClick={onToggle} aria-expanded={open}>
          <span className="rv-kind" aria-hidden="true"><Ic size={15} /></span>
          <div className="rv-row-main">
            <div className="rv-meta">
              <Tag tone={RV_TONE[m.tone]} dot>{m.label}</Tag>
              {item.qualityLevel && (
                <Tag tone={blocking ? "danger" : "warn"} title={blocking ? "阻断级：处理前不能归档（正文已保留）" : "建议级：不拦归档，按需修改"}>
                  {blocking ? "阻断" : "建议"}
                </Tag>
              )}
              {item.where && <span className="rv-where">{item.where}</span>}
            </div>
            <h3 className="rv-item-title" title={item.title}>{item.title}</h3>
          </div>
          <span className="rv-time">{item.time}</span>
          <span className="rv-chev" data-open={open} aria-hidden="true"><I.ChevronDown size={16} /></span>
        </button>

        <div className="rv-detail" data-open={open}>
          <div className="rv-detail-inner">
            {item.detail && <p className="rv-detail-text">{item.detail}</p>}

            {item.preview && (
              <div className="rv-preview">
                <div className="rv-preview-row"><span className="rv-preview-tag">原</span><span className="rv-preview-old">{item.preview.before}</span></div>
                <div className="rv-preview-row"><span className="rv-preview-tag is-new">改</span><span className="rv-preview-new">{item.preview.after}</span></div>
              </div>
            )}

            {item.checklist && (
              <ul className="rv-checklist">
                {item.checklist.map((c, i) => <li key={i}><I.Circle size={11} /> {c}</li>)}
              </ul>
            )}

            {item.options && (
              <div className="rv-options">
                {item.options.map((o, i) => <span key={i} className="rv-option">{o}</span>)}
              </div>
            )}

            {item.source && <div className="rv-src">来自{item.source}</div>}
          </div>
        </div>

        <div className="rv-actions">
          {actions.map((a, i) => {
            const cls = a.intent === "primary" ? "btn btn-accent btn-sm"
              : a.intent === "ghost" ? "btn btn-ghost btn-sm" : "btn btn-quiet btn-sm";
            return <button type="button" key={i} className={cls} onClick={() => onAct(a)}>{a.label}</button>;
          })}
        </div>
      </div>
    </article>
  );
}

export function RvEmpty({ hasSnoozed, go }) {
  return (
    <EmptyState
      className="rv-empty"
      icon="CheckCircle"
      title="收件箱清空了"
      actions={<button type="button" className="btn btn-accent" onClick={() => go("writer")}><I.Pen size={15} /> 进入写作房间</button>}
    >
      {hasSnoozed ? "当前待办都处理完了，还有几条在「稍后处理」里等着。" : "需要你拍板的都处理完了，回到写作房间继续吧。"}
    </EmptyState>
  );
}
