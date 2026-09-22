import React from "react";

/* ==========================================================
   设置页的小零件：Section（一个卡片区块）、Row（标签 + 控件的一行）、Toggle（开关）、Field（接上 Row 标签的输入控件）。
   Row 用 <label htmlFor> 给控件一个可读的名字：同一行里的 Field / Toggle 从上下文拿到 id，
   屏幕阅读器读到的是「稿纸纹理，开关，已打开」，而不是十个一模一样的「切换」。
   分段单选用共享的 ws-ui.jsx Segmented（radiogroup），这里不再有私有版本。
   ========================================================== */

const cx = (...parts) => parts.filter(Boolean).join(" ");
const RowContext = React.createContext(null);

function useSetRow() {
  return React.useContext(RowContext) || {};
}

function Section({ title, desc, children, className, id }) {
  return (
    <section className={cx("set-section", className)} id={id}>
      <header className="set-section-head">
        <h2 className="set-section-title text-serif">{title}</h2>
        {desc && <p className="set-section-desc">{desc}</p>}
      </header>
      <div className="set-section-body">{children}</div>
    </section>
  );
}

/* stacked：标签在上、控件占满整行（长文本、网址、模型列表）；readonly：右侧只是文字，不是控件 */
function Row({ label, hint, children, stacked = false, readonly = false, className, controlId }) {
  const autoId = React.useId();
  const id = controlId || `set-${autoId.replace(/:/g, "")}`;
  const ctx = React.useMemo(() => ({
    controlId: id,
    labelId: `${id}-label`,
    hintId: hint ? `${id}-hint` : undefined,
  }), [id, hint]);
  return (
    <div className={cx("set-row", stacked && "is-stacked", className)}>
      <div className="set-row-text">
        {readonly
          ? <div className="set-row-label" id={ctx.labelId}>{label}</div>
          : <label className="set-row-label" id={ctx.labelId} htmlFor={id}>{label}</label>}
        {hint && <div className="set-row-hint" id={ctx.hintId}>{hint}</div>}
      </div>
      <div className="set-row-ctl">
        <RowContext.Provider value={ctx}>{children}</RowContext.Provider>
      </div>
    </div>
  );
}

/* 输入控件：默认拿 Row 的 id 与说明；同一行第二个控件传 id={undefined} 并自带 aria-label */
function Field({ as: Tag = "input", ...props }) {
  const row = useSetRow();
  return <Tag id={row.controlId} aria-describedby={row.hintId} {...props} />;
}

/* 开关：role=switch + aria-checked；名字来自所在 Row 的标签，或显式的 label（同时显示在开关旁边） */
function Toggle({ on, onChange, label, showLabel = false, disabled = false, id }) {
  const row = useSetRow();
  return (
    <span className={cx("toggle-wrap", disabled && "is-disabled")}>
      <button
        type="button"
        role="switch"
        id={id !== undefined ? id : (label ? undefined : row.controlId)}
        aria-checked={!!on}
        aria-label={label || undefined}
        aria-labelledby={label ? undefined : row.labelId}
        disabled={disabled}
        className={`toggle ${on ? "is-on" : ""}`}
        onClick={() => onChange(!on)}
      >
        <span className="toggle-knob" />
      </button>
      {showLabel && label && <span className="toggle-text" aria-hidden="true">{label}</span>}
    </span>
  );
}

export { Section, Row, Field, Toggle, useSetRow };
