import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { wsToast } from "./ws-notify.jsx";
import { useStoreTick } from "./lib/store-utils.js";
import { EmptyState } from "./ws-ui.jsx";
import { srErrorInfo } from "./ws-styleref-model.js";
import { srConfigureHost, srSubscribe } from "./ws-styleref-store.js";

/* ==========================================================
   风格参考 · 各页共用的界面零件
   · srNotify / srActiveWork —— 提示、当前作品；当前作品在模块加载时经 srConfigureHost 交给 store
   · useSrStore —— 订阅 store 的频道重渲（lib/store-utils 的 useStoreTick）
   · SrErrorLine —— 出错的一句话 + 下一步按钮（去设置模型 / 打开这本 / 去学习文风）
   · SrMenu（页头「更多」）、SrStageEmpty（缺前一步时的空态卡）、SrProgressBar（进度条）
   不写 window。
   ========================================================== */

/* 失败 / 回执提示：外壳的提示层挂着时走应用内提示（ws-notify），没挂（单测里单独渲染）时退回 alert。 */
export function srNotify(message, tone = "danger") {
  if (wsToast({ message, tone })) return;
  try { window.alert(message); } catch (e) { /* 无头环境 */ }
}

/* 出错时的一句话提示（按错误码给中文，见 srErrorInfo） */
export function srNotifyError(error, fallback) {
  srNotify(srErrorInfo(error, fallback).message);
}

/* 当前作品：书架还在加载（__loading__）或为空时返回 null */
export function srActiveWork() {
  try {
    const w = WsWorks && typeof WsWorks.active === "function" ? WsWorks.active() : null;
    if (w && w.id && w.id !== "__loading__") return { id: w.id, title: w.title || "" };
    const id = WsWorks && typeof WsWorks.activeId === "function" ? WsWorks.activeId() : null;
    return id && id !== "__loading__" ? { id, title: "" } : null;
  } catch (e) {
    return null;
  }
}

srConfigureHost({
  activeWorkId: () => { const w = srActiveWork(); return w ? w.id : null; },
});

/* 订阅 store 的若干频道（books / detail / activity），有变化就重渲 */
export function useSrStore(...channels) {
  useStoreTick(srSubscribe(...channels));
}

/* 出错的一句话 + 下一步。onAction(action) 由页面决定怎么走（去设置、打开书、跳到学习文风）。 */
export function SrErrorLine({ error, onAction, className, testId }) {
  if (!error) return null;
  const info = srErrorInfo(error);
  return (
    <p className={`sr-error-line${className ? ` ${className}` : ""}`} role="alert" data-testid={testId} title={info.code ? `错误代码：${info.code}` : undefined}>
      <I.AlertTriangle size={13} aria-hidden="true" />
      <span>{info.message}</span>
      {info.action && onAction && (
        <button type="button" className="btn btn-quiet btn-xs" data-testid={testId ? `${testId}-action` : undefined} onClick={() => onAction(info.action)}>
          {info.action.label}
        </button>
      )}
    </p>
  );
}

/* 进度条：percent 0–100 */
export function SrProgressBar({ percent, label }) {
  return (
    <div className="sr-progress" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
      <span className="sr-progress-fill" style={{ width: `${percent}%` }} />
    </div>
  );
}

/* 缺前一步时的空态卡：说现状，给下一步 */
export function SrStageEmpty({ icon = "Sparkles", title, children, actionLabel, onAction, testId }) {
  return (
    <div className="card sr-stage-empty-card" data-testid={testId}>
      <EmptyState icon={icon} title={title} actions={actionLabel ? <button type="button" className="btn btn-accent btn-sm" onClick={onAction}>{actionLabel}</button> : null}>
        {children}
      </EmptyState>
    </div>
  );
}

/* 页头「更多」菜单：按钮 + role=menu 弹层；方向键在项间移动，Esc / 点外面关闭并把焦点还给按钮。 */
export function SrMenu({ label, items }) {
  const [open, setOpen] = React.useState(false);
  const wrapRef = React.useRef(null);
  const btnRef = React.useRef(null);
  const menuId = React.useId();
  React.useEffect(() => {
    if (!open) return undefined;
    const first = wrapRef.current && wrapRef.current.querySelector('[role="menuitem"]:not([disabled])');
    if (first) first.focus();
    const onDown = (e) => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); setOpen(false); if (btnRef.current) btnRef.current.focus(); }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const onMenuKey = (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    const nodes = Array.from(e.currentTarget.querySelectorAll('[role="menuitem"]:not([disabled])'));
    if (!nodes.length) return;
    e.preventDefault();
    const at = nodes.indexOf(document.activeElement);
    let next = at;
    if (e.key === "Home") next = 0;
    else if (e.key === "End") next = nodes.length - 1;
    else next = (at + (e.key === "ArrowDown" ? 1 : -1) + nodes.length) % nodes.length;
    nodes[next].focus();
  };
  return (
    <div className="sr-menu" ref={wrapRef}>
      <button
        ref={btnRef}
        type="button"
        className="btn btn-ghost btn-sm btn-icon"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((o) => !o)}
      ><I.More size={15} /></button>
      {open && (
        <div className="sr-menu-pop" role="menu" id={menuId} aria-label={label} onKeyDown={onMenuKey}>
          {items.map((it) => {
            const Ic = it.icon ? I[it.icon] : null;
            return (
              <button
                key={it.id}
                type="button"
                role="menuitem"
                className={`sr-menu-item${it.danger ? " is-danger" : ""}`}
                data-testid={it.testId}
                disabled={it.disabled}
                title={it.title}
                onClick={() => {
                  // 先把焦点还给「更多」按钮再执行：菜单项马上卸载，随后打开的确认框要记住正确的「打开前焦点」
                  setOpen(false);
                  if (btnRef.current) btnRef.current.focus();
                  if (it.onSelect) it.onSelect();
                }}
              >
                {Ic ? <Ic size={14} /> : null}<span>{it.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
