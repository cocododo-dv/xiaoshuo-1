import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";

const { useEffect, useMemo, useRef, useState } = React;

function exactFindingCode(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 128
    && value === value.trim()
    ? value
    : null;
}

// 只读取后端 CONTENT_SAFETY_REVIEW_REQUIRED 信封中的原始 finding code。
// 不接受 blocker 前缀、warning issue_key 或客户端自造值，避免把普通警告扩成绕过令牌。
function contentSafetyReviewFromError(error) {
  if (!error || error.code !== "CONTENT_SAFETY_REVIEW_REQUIRED") return null;
  const gate = error.details && error.details.final_text_gate;
  const safety = gate && gate.content_safety;
  const raw = safety && Array.isArray(safety.findings) ? safety.findings : [];
  const seen = new Set();
  const findings = raw.flatMap((finding) => {
    if (!finding || finding.review_required !== true || finding.acknowledged === true) return [];
    const code = exactFindingCode(finding.code);
    if (!code || seen.has(code)) return [];
    seen.add(code);
    return [{
      code,
      severity: typeof finding.severity === "string" ? finding.severity : "unknown",
      confidence: typeof finding.confidence === "string" ? finding.confidence : "heuristic",
      message: typeof finding.message === "string" && finding.message.trim()
        ? finding.message.trim()
        : "该内容风险需要作者人工核对。",
      evidenceTerms: Array.isArray(finding.evidence_terms)
        ? finding.evidence_terms.filter(term => typeof term === "string" && term.trim()).map(term => term.trim()).slice(0, 8)
        : [],
    }];
  });
  if (!findings.length) return null;
  return {
    findings,
    limitations: Array.isArray(safety.limitations)
      ? safety.limitations.filter(item => typeof item === "string" && item.trim()).map(item => item.trim()).slice(0, 4)
      : [],
  };
}

/* 严重度 / 判定方式在服务端是英文枚举；给作者看中文 */
const SEVERITY_LABEL = { critical: "极高", high: "高", medium: "中", low: "低" };
const CONFIDENCE_LABEL = { heuristic: "启发式规则", model: "模型判断", llm: "模型判断", rule: "规则" };

/* 内容风险逐项确认。建在共享的 WsDialog 上：焦点陷阱、Esc / 点遮罩等于「返回修改」、
   关闭后焦点回到打开前的元素、遮罩用 --scrim（夜间是压暗而不是泛白）。
   正在重新校验时，Esc 与遮罩都不关（onBeforeClose 拦下）。 */
function ContentSafetyReviewDialog({ review, busy = false, error = "", onCancel, onConfirm }) {
  const [checked, setChecked] = useState(() => new Set());
  const cancelRef = useRef(null);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const findings = (review && review.findings) || [];
  useEffect(() => { setChecked(new Set()); }, [review]);

  const acceptedCodes = useMemo(
    () => findings.filter(item => checked.has(item.code)).map(item => item.code),
    [checked, findings],
  );
  const allConfirmed = findings.length > 0 && acceptedCodes.length === findings.length;
  const toggle = (code) => {
    if (busy) return;
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(code)) next.delete(code); else next.add(code);
      return next;
    });
  };

  return (
    <WsDialog
      onClose={() => onCancel()}
      onBeforeClose={() => !busyRef.current}
      labelledBy="wr-safety-title"
      describedBy="wr-safety-desc"
      size="lg"
      className="wr-safety-dialog"
      scrimClassName="wr-safety-scrim"
      initialFocus={cancelRef}
    >
      <header className="wr-safety-head">
        <span className="wr-safety-shield" aria-hidden="true"><I.ShieldCheck size={19} /></span>
        <div className="wr-safety-heading">
          <h2 id="wr-safety-title">提升前请逐项核对内容风险</h2>
          <p id="wr-safety-desc">这是启发式规则的提醒，不是判决。系统不会替你勾选；每一项都确认过，才会重新校验并提升正文。</p>
        </div>
        <button ref={cancelRef} type="button" className="wr-safety-close" onClick={onCancel} disabled={busy} aria-label="返回修改，不提升"><I.X size={18} /></button>
      </header>

      <div className="wr-safety-findings" role="group" aria-label="需要作者确认的内容风险">
        {findings.map((finding, index) => (
          <label className={`wr-safety-finding ${checked.has(finding.code) ? "is-checked" : ""}`} key={finding.code} data-code={finding.code}>
            <input
              type="checkbox"
              checked={checked.has(finding.code)}
              onChange={() => toggle(finding.code)}
              disabled={busy}
              aria-describedby={`wr-safety-finding-${index}`}
            />
            <span className="wr-safety-check" aria-hidden="true"><I.Check size={13} /></span>
            <span className="wr-safety-copy" id={`wr-safety-finding-${index}`}>
              <span className="wr-safety-message">{finding.message}</span>
              {finding.evidenceTerms.length > 0 && (
                <span className="wr-safety-evidence"><b>命中的词</b>{finding.evidenceTerms.map(term => <em key={term}>{term}</em>)}</span>
              )}
              <small>严重程度：{SEVERITY_LABEL[finding.severity] || "未标注"}　判定方式：{CONFIDENCE_LABEL[finding.confidence] || "未标注"}</small>
            </span>
          </label>
        ))}
      </div>

      {(review.limitations || []).length > 0 && (
        <details className="wr-safety-limits">
          <summary>这类规则有哪些盲区</summary>
          <ul>{review.limitations.map(item => <li key={item}>{item}</li>)}</ul>
        </details>
      )}

      <footer className="wr-safety-foot">
        <div className="wr-safety-progress" role="status" aria-live="polite">
          已核对 {acceptedCodes.length} / {findings.length} 项
          {error && <span role="alert">{error}</span>}
        </div>
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>返回修改</button>
        <button
          type="button"
          className="btn btn-accent"
          disabled={!allConfirmed || busy}
          onClick={() => onConfirm(findings.map(item => item.code))}
          data-testid="content-safety-confirm"
        >
          <I.CheckCircle size={14} /> {busy ? "正在重新校验…" : "都已核对，重新校验并提升"}
        </button>
      </footer>
    </WsDialog>
  );
}

export { ContentSafetyReviewDialog, contentSafetyReviewFromError };
