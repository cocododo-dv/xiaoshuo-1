import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { wsConfirm, wsToast } from "./ws-notify.jsx";
import { useStoreTick } from "./lib/store-utils.js";
import { EmptyState } from "./ws-ui.jsx";
import { srConfigureHost, srDeepFor, srLoadDeep, srSubscribe } from "./ws-styleref-store.js";

/* ==========================================================
   风格参考 · 各 stage 共用的界面零件
   · srNotify / srActiveWork / srWorkTitle —— 提示、当前作品、作品名；
     模块加载时经 srConfigureHost 交给 store（store 自己不 import 界面模块）
   · useSrStore / useSrDeep —— 订阅 store 的频道重渲（lib/store-utils 的 useStoreTick），
     useSrDeep 顺带懒加载这本书的深层数据
   · SrMenu（页头「更多」）、SrStageEmpty（缺产物时的空态卡）、SrProgressBar（活动与概览的进度条）
   不写 window。
   ========================================================== */

/* 失败 / 回执提示：外壳的提示层挂着时走应用内提示（ws-notify），没挂（单测里单独渲染）时退回 alert。 */
export function srNotify(message, tone = "danger") {
  if (wsToast({ message, tone })) return;
  try { window.alert(message); } catch (e) { /* 无头环境 */ }
}

/* 当前作品（「用于当前作品」的判断、项目级绑定的名字）。书架还在加载（__loading__）或为空时返回 null。 */
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

/* 作品 id → 书名（绑定行、叠层行不印 PRJ_… 原始 id）；认不出返回 null。 */
export function srWorkTitle(workId) {
  if (!workId) return null;
  try {
    const list = WsWorks && typeof WsWorks.list === "function" ? WsWorks.list() : [];
    const hit = (list || []).find((w) => w && w.id === workId);
    if (hit && hit.title) return hit.title;
  } catch (e) { /* 忽略 */ }
  const active = srActiveWork();
  return active && active.id === workId && active.title ? active.title : null;
}

srConfigureHost({
  activeWorkId: () => { const w = srActiveWork(); return w ? w.id : null; },
  notify: srNotify,
  confirm: wsConfirm,
});

/* 订阅 store 的若干频道（books / deep / activity），有变化就重渲。频道由调用方写成字面量。 */
export function useSrStore(...channels) {
  useStoreTick(srSubscribe(...channels));
}

/* 这本书的深层数据：读缓存，没有就懒加载；缺数据的地方由各 stage 显示空态，不回退任何示例数据。 */
export function useSrDeep(book) {
  useSrStore("deep");
  const bookId = book ? book.id : null;
  React.useEffect(() => { if (bookId) srLoadDeep(bookId); }, [bookId]);
  return bookId ? srDeepFor(bookId) : null;
}

/* 进度条（参考书活动、概览的分类 / 抽取进度共用）：percent 0–100 */
export function SrProgressBar({ percent, label }) {
  return (
    <div className="sr-import-bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
      <span className="sr-import-bar-fill" style={{ width: `${percent}%` }} />
    </div>
  );
}

/* 缺产物时的空态卡（画像 / 回测 / 注入共用）：说现状，给下一步。 */
export function SrStageEmpty({ icon = "Sparkles", title, children, actionLabel, onAction }) {
  return (
    <div className="card sr-stage-empty-card">
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
                  // 先把焦点还给「更多」按钮再执行：菜单项马上卸载，随后打开的确认框会把「打开前的焦点」
                  // 记成 body，取消后焦点就落到页面顶端去了。
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
