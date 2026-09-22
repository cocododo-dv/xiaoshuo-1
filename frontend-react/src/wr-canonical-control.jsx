import React from "react";

const CANONICAL_LABELS = {
  none: "还没有可提升的正文",
  unknown: "权威正文状态待确认",
  dirty: "权威正文待更新",
  promoting: "正在提升权威正文…",
  review: "内容风险待作者复核",
  current: "权威正文已更新",
  reconcile: "需先核对事实变更",
  error: "权威正文提升失败",
};

/* none：这一场一个字都没有。不显示「待更新」这种暗示有事要办的状态，也不给提升按钮——
   空白场没有可提升的正文。状态文字仍留给读屏（ws-sr-only），按钮留在原位但隐藏，
   保证它始终是这个控件里的第一个 <button>（调用方与单测按这个位置找它）。 */
function WrCanonicalControl({ saveStatus, canonicalStatus, disabled = false, onPromote }) {
  const status = CANONICAL_LABELS[canonicalStatus] ? canonicalStatus : "unknown";
  const busy = status === "promoting";
  const none = status === "none";
  const saveFailed = saveStatus === "草稿保存失败";
  const promotionDisabled = disabled || busy || none || status === "review" || status === "unknown" || status === "current";
  return (
    <div className="wr-publish-state" aria-label="草稿与权威正文状态">
      <span role="status" aria-live="polite" aria-atomic="true" className={`wr-save ${saveStatus === "草稿已保存" ? "" : "saving"} ${saveFailed ? "is-error" : ""}`} data-testid="draft-save-status" title={saveStatus}>
        <span className="wr-save-dot" aria-hidden="true" /><span className="wr-save-text">{saveStatus}</span>
      </span>
      <span role="status" aria-live="polite" aria-atomic="true" className={`wr-canonical-state is-${status}${none ? " ws-sr-only" : ""}`} data-testid="canonical-status" title={CANONICAL_LABELS[status]}>
        <span className="wr-canonical-dot" aria-hidden="true" /><span className="wr-canonical-text">{CANONICAL_LABELS[status]}</span>
      </span>
      <button
        type="button"
        className="wr-canonical-promote"
        disabled={promotionDisabled}
        hidden={none}
        onClick={onPromote}
        title="只在这次修改没有改变故事事实时使用：把已保存的草稿提升为运行时的权威正文"
      >
        {busy ? "提升中…" : "提升为权威正文"}
      </button>
    </div>
  );
}

export { CANONICAL_LABELS, WrCanonicalControl };
