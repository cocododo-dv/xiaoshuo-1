import React from "react";
import ReactDOM from "react-dom";

/* ==========================================================
   UndoToast — 删除类动作的统一回执
   ----------------------------------------------------------
   删除的交互难点从来不是「怎么删」，而是「删完之后东西去哪了」。
   原来三个视图删完一片安静，作者只能靠猜；确认弹窗又只在动手前出现，
   帮不上事后的忙。这里给一条统一回执：说清删了几条、去了哪儿，
   并把「撤销 / 打开回收站」放在手边。

   两种回执：
   · 可撤销（起草台移出队列这类纯本地动作）→ 给「撤销」，点了立刻还原；
   · 不可即时撤销（章/场软删，需要服务端 restore）→ 给「打开回收站」，
     诚实地把恢复入口指出来，不假装一键还原。
   ========================================================== */

const TOAST_MS = 6000;

export function useUndoToast() {
  const [toast, setToast] = React.useState(null);
  const timerRef = React.useRef(null);
  const clear = React.useCallback(() => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    setToast(null);
  }, []);
  /* show({ text, actionLabel, onAction, tone }) —— onAction 执行后回执自动收起 */
  const show = React.useCallback((next) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setToast(next ? { ...next, key: Date.now() } : null);
    if (next) timerRef.current = setTimeout(() => setToast(null), next.timeout || TOAST_MS);
  }, []);
  React.useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);
  return { toast, show, clear };
}

/* 提示栈：外壳的提示层（ws-notify.jsx 的 WsToastHost）挂载时登记一个容器（.ws-toast-stack，屏幕底部居中的一列），
   视图自己的回执和外壳的提示都进这一列、一条摞一条——过去它们各自 fixed 在同一块像素上，后来的盖住先来的
   （连同先来那条上的「撤销」）。没有登记（单测里单独渲染某个视图）时照旧就地渲染。只存一个元素，不写 window。 */
let toastStackEl = null;
const toastStackSubs = new Set();

export function registerToastStack(el) {
  toastStackEl = el || null;
  toastStackSubs.forEach((fn) => { try { fn(); } catch (e) {} });
  return () => {
    if (toastStackEl !== el) return;
    toastStackEl = null;
    toastStackSubs.forEach((fn) => { try { fn(); } catch (e) {} });
  };
}

function subscribeToastStack(fn) {
  toastStackSubs.add(fn);
  return () => { toastStackSubs.delete(fn); };
}

function useToastStack() {
  return React.useSyncExternalStore(subscribeToastStack, () => toastStackEl, () => null);
}

/* 外壳的提示层（ws-notify.jsx）也用这一份外观：失败类提示（tone="danger"）以 role="alert" 播报。
   inline：就地渲染、不进提示栈（提示层自己已经把整组提示放进栈里了）。 */
export function UndoToast({ toast, onClose, inline = false }) {
  const stack = useToastStack();
  if (!toast) return null;
  const urgent = toast.tone === "danger";
  const node = (
    <div className={`ws-toast tone-${toast.tone || "neutral"}`} role={urgent ? "alert" : "status"}
      aria-live={urgent ? "assertive" : "polite"} data-testid="undo-toast">
      <span className="ws-toast-text">{toast.text}</span>
      {toast.actionLabel && (
        <button type="button" className="ws-toast-action" data-testid="undo-toast-action"
          onClick={() => { if (toast.onAction) toast.onAction(); if (onClose) onClose(); }}>
          {toast.actionLabel}
        </button>
      )}
      <button type="button" className="ws-toast-x" aria-label="关闭提示" onClick={onClose}>×</button>
    </div>
  );
  return stack && !inline ? ReactDOM.createPortal(node, stack) : node;
}

export default UndoToast;
