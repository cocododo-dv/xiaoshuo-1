import React from "react";
import ReactDOM from "react-dom";

/* 共享对话框原语（2026-09-21 前端重构）。
   以前每个视图各写一遍：窗口级 Esc 监听、没有焦点陷阱、关闭后焦点丢在 body、
   遮罩用 --ink-1 调色导致夜间主题变亮、z-index 各自为政被悬浮按钮压住。
   这里统一：role="dialog" + aria-modal、初始焦点、Tab 循环、Esc / 遮罩关闭都走
   同一个 requestClose（可被 onBeforeClose 拦下，用于「有未保存改动」确认）、
   关闭后把焦点还给打开它的元素、叠放时只有最上层响应 Esc。
   纯 ESM，不写 window。 */

const { useEffect, useLayoutEffect, useRef, useCallback } = React;

const FOCUSABLE = [
  "button:not([disabled])", "[href]", "input:not([disabled]):not([type=hidden])",
  "select:not([disabled])", "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
  "[contenteditable='true']",
].join(",");

export function focusableIn(root) {
  if (!root) return [];
  return [...root.querySelectorAll(FOCUSABLE)]
    .filter((node) => !node.hidden && node.getAttribute("aria-hidden") !== "true" && !node.closest("[inert]"));
}

/* 输入法组字中的回车 / 方向键不是命令（拼音选词时按回车会误触发提交）。 */
export function isImeComposing(event) {
  if (!event) return false;
  const native = event.nativeEvent || event;
  return Boolean(native.isComposing || event.isComposing || native.keyCode === 229 || event.keyCode === 229);
}

/* 当前打开的对话框栈：只有栈顶响应 Esc / 焦点陷阱。每一项记着它的根节点（topModalLayer 用）。 */
const dialogStack = [];

/* 最上层的模态层（WsDialog、续写托盘、窄屏抽屉……凡是激活了 useFocusTrap 的都算）的根节点，没有就是 null。
   全局快捷键（⌘K、写作台的 ⌘J / ⌘1 …）用它判断背后的页面此刻该不该响应：键只作用于最上层——
   过去确认框开着时 ⌘K 照样在它底下打开命令面板，回车就把整个应用跳走了。 */
export function topModalLayer() {
  const top = dialogStack[dialogStack.length - 1];
  return (top && top.root) || null;
}

export function useFocusTrap(ref, active = true, { initialFocus, restoreFocus = true } = {}) {
  // 打开者存在 ref 里、只记第一次：StrictMode 会把效果跑两遍，第二遍时焦点已经在对话框里，
  // 若每遍都读 activeElement，就会把对话框自己的第一个按钮当成「打开者」（关闭后焦点掉进 body）。
  const openerRef = useRef(null);
  // 每次激活自增：清理里排队的还焦点若发现效果又跑了一遍（StrictMode 或立刻重新打开），就作废，
  // 否则第一遍的清理会把焦点从刚打开的对话框里抢回到背后的按钮上。
  const generationRef = useRef(0);
  useLayoutEffect(() => {
    if (!active) return undefined;
    const root = ref.current;
    if (!root) return undefined;
    const generation = ++generationRef.current;
    const current = typeof document !== "undefined" ? document.activeElement : null;
    if (!openerRef.current && current && current !== document.body && !root.contains(current)) openerRef.current = current;
    const token = { root };
    dialogStack.push(token);
    const target = (initialFocus && initialFocus.current) || focusableIn(root)[0] || root;
    if (target && typeof target.focus === "function") target.focus({ preventScroll: true });

    const onKey = (event) => {
      if (event.key !== "Tab" || dialogStack[dialogStack.length - 1] !== token) return;
      const nodes = focusableIn(root);
      if (!nodes.length) { event.preventDefault(); root.focus(); return; }
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      const index = dialogStack.indexOf(token);
      if (index >= 0) dialogStack.splice(index, 1);
      // 放到微任务里：React 提交后会把焦点还给提交前聚焦的元素（restoreSelection）。
      // 常驻的陷阱（例如窄屏抽屉只是隐藏）里那个元素仍在文档中，同步还焦点会被它抢回去。
      queueMicrotask(() => {
        if (generationRef.current !== generation) return; // 已经重新激活：不是真的关闭
        const opener = openerRef.current;
        openerRef.current = null;
        if (!restoreFocus || !opener || typeof opener.focus !== "function" || !document.contains(opener)) return;
        const now = document.activeElement;
        if (now && now !== document.body && !root.contains(now)) return; // 作者已经去了别处
        opener.focus({ preventScroll: true });
      });
    };
    // initialFocus 是 ref，不参与依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
}

/* WsDialog — 通用模态框。
   props:
     open (默认 true)、onClose()、label | labelledBy、describedBy、
     size: "sm" | "md" | "lg" | "xl"、className（加在面板上）、scrimClassName、
     dismissOnBackdrop（默认 true）、onBeforeClose() → false / Promise<false> 时不关、
     initialFocus（ref）、testId、zIndex（嵌套时覆盖）、as（面板标签，默认 section）、
     portal（默认 true；false 时就地渲染——遮罩仍是 fixed 全屏，焦点陷阱与 Esc 栈照常，
     用于必须留在宿主 DOM 里的面板，例如测试在容器内查询的章节编排面板）。 */
export function WsDialog({
  open = true, onClose, label, labelledBy, describedBy, size = "md", className = "",
  scrimClassName = "", dismissOnBackdrop = true, onBeforeClose, initialFocus, testId,
  zIndex, as: Panel = "section", portal = true, children,
}) {
  const panelRef = useRef(null);
  const tokenRef = useRef(null);
  useFocusTrap(panelRef, open, { initialFocus });

  const requestClose = useCallback(async (reason) => {
    if (onBeforeClose) {
      const verdict = await onBeforeClose(reason);
      if (verdict === false) return;
    }
    if (onClose) onClose(reason);
  }, [onBeforeClose, onClose]);

  // 最新的 requestClose 走 ref：监听只在打开时注册一次，栈顶令牌也只在打开时取一次——
  // 否则父对话框因回调换了身份而重新订阅时，会把已经打开的子对话框的令牌当成自己的。
  const closeRef = useRef(requestClose);
  closeRef.current = requestClose;
  useEffect(() => {
    if (!open) return undefined;
    tokenRef.current = dialogStack[dialogStack.length - 1];
    const onKey = (event) => {
      if (event.key !== "Escape" || event.defaultPrevented || isImeComposing(event)) return;
      if (dialogStack[dialogStack.length - 1] !== tokenRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      void closeRef.current("escape");
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);

  if (!open || typeof document === "undefined") return null;
  const style = zIndex != null ? { zIndex } : undefined;
  const tree = (
    <div
      className={`ws-dialog-scrim ${scrimClassName}`.trim()}
      style={style}
      onMouseDown={(event) => {
        if (event.target !== event.currentTarget) return;
        // 遮罩不可聚焦：不阻止默认行为的话，按下遮罩会让焦点掉到 body，关不掉时焦点就逃出了对话框。
        event.preventDefault();
        if (dismissOnBackdrop) void requestClose("backdrop");
      }}
    >
      <Panel
        ref={panelRef}
        className={`ws-dialog ws-dialog-${size} ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-label={labelledBy ? undefined : label}
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        tabIndex={-1}
        data-testid={testId}
      >
        {typeof children === "function" ? children({ requestClose }) : children}
      </Panel>
    </div>
  );
  return portal ? ReactDOM.createPortal(tree, document.body) : tree;
}
