import React from "react";
import ReactDOM from "react-dom";
import { isImeComposing } from "./lib/keyboard.js";

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

/* 输入法组字判定住在 lib/keyboard.js；这里转出，旧的导入路径照旧可用。 */
export { isImeComposing };

/* 当前打开的层栈：模态层（useFocusTrap：WsDialog、续写托盘、窄屏抽屉……）与非模态浮层（usePopover：
   弹出菜单、筛选面板……）按打开顺序叠放。Esc 只归栈顶那一层；焦点陷阱只看最上面的模态层（浮层开在
   对话框里时，Tab 仍在对话框里循环）。每一项记着它的根节点（topModalLayer 用），浮层项带 modal: false。 */
const dialogStack = [];

function topModalToken() {
  for (let i = dialogStack.length - 1; i >= 0; i -= 1) {
    if (dialogStack[i].modal !== false) return dialogStack[i];
  }
  return null;
}

/* 最上层的模态层（WsDialog、续写托盘、窄屏抽屉……凡是激活了 useFocusTrap 的都算）的根节点，没有就是 null。
   全局快捷键（⌘K、写作台的 ⌘J / ⌘1 …）用它判断背后的页面此刻该不该响应：键只作用于最上层——
   过去确认框开着时 ⌘K 照样在它底下打开命令面板，回车就把整个应用跳走了。非模态浮层不算（它开着时
   快捷键照常；Esc 由浮层自己先接住，见 usePopover）。 */
export function topModalLayer() {
  const top = topModalToken();
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
      if (event.key !== "Tab" || topModalToken() !== token) return;
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

/* usePopover — 非模态浮层（弹出菜单、筛选面板、就地小表单）的开合行为，一处写全：
   · 打开时进层栈（modal: false）：Esc 只在它是栈顶时由它接住（开在对话框里的浮层先关自己，对话框不动），
     输入法组字中的 Esc 不算；Esc 关上后把焦点还给 anchorRef（打开它的按钮）；
   · 在浮层与 anchor 之外按下指针（pointerdown）、或焦点移到它们之外（Tab 出去）就关，焦点留在作者去的地方；
     上面还叠着别的层（例如从浮层里打开的确认框）时，外面的按下与焦点移动都归那一层，不关浮层；
   · 打开时焦点进浮层：initialFocus → 第一个可聚焦的控件 → 浮层本身（focusOnOpen: false 时留在原处）；
   · 浮层被父组件直接收起、焦点随之掉进 <body> 时，还给 anchor。
   onClose(reason) 的 reason：escape | outside | focusout，或调用返回的 close 时调用方给的（默认 close）。
   返回 close(reason, { restoreFocus = true })：由浮层里的动作收起它（菜单项选中、表单提交）。
   纯 DOM，不写 window。样式与定位由调用方（或 ws-ui 的 Popover / MenuButton）负责。 */
export function usePopover(ref, { open, onClose, anchorRef, initialFocus, focusOnOpen = true, closeOnFocusOut = true } = {}) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // 开关项走 ref：只在打开时读一次，浮层开着时父组件换了取值也不会把它拆下重装（重装会重新抢焦点）
  const optionsRef = useRef({ focusOnOpen, closeOnFocusOut });
  optionsRef.current = { focusOnOpen, closeOnFocusOut };

  const focusAnchor = useCallback(() => {
    const anchor = anchorRef && anchorRef.current;
    if (anchor && typeof anchor.focus === "function" && document.contains(anchor)) anchor.focus({ preventScroll: true });
  }, [anchorRef]);

  const close = useCallback((reason = "close", { restoreFocus = true } = {}) => {
    if (closeRef.current) closeRef.current(reason);
    if (restoreFocus) focusAnchor();
  }, [focusAnchor]);

  useLayoutEffect(() => {
    if (!open) return undefined;
    const root = ref.current;
    if (!root) return undefined;
    const token = { root, modal: false };
    dialogStack.push(token);
    const isTop = () => dialogStack[dialogStack.length - 1] === token;
    const inside = (node) => {
      const anchor = anchorRef && anchorRef.current;
      return !!node && (root.contains(node) || !!(anchor && anchor.contains(node)));
    };
    if (optionsRef.current.focusOnOpen) {
      const target = (initialFocus && initialFocus.current) || focusableIn(root)[0] || root;
      if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
    }
    // 焦点此刻在不在浮层里（收起时据此决定要不要把焦点还给 anchor）
    let focusInside = root.contains(document.activeElement);
    const onKey = (event) => {
      if (event.key !== "Escape" || event.defaultPrevented || isImeComposing(event) || !isTop()) return;
      event.preventDefault();
      event.stopPropagation();
      if (closeRef.current) closeRef.current("escape");
      focusAnchor();
    };
    const onPress = (event) => {
      if (!isTop() || inside(event.target)) return;
      if (closeRef.current) closeRef.current("outside");
    };
    const onFocusIn = (event) => {
      focusInside = root.contains(event.target);
      if (!optionsRef.current.closeOnFocusOut || !isTop() || inside(event.target)) return;
      if (closeRef.current) closeRef.current("focusout");
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onPress, true);
    document.addEventListener("focusin", onFocusIn, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onPress, true);
      document.removeEventListener("focusin", onFocusIn, true);
      const index = dialogStack.indexOf(token);
      if (index >= 0) dialogStack.splice(index, 1);
      // 浮层里有焦点时被收起：焦点随节点一起掉进 <body>，还给打开它的按钮（与 useFocusTrap 一样放到微任务里）。
      // 焦点本来就不在浮层里（点了页面空白处、focusOnOpen: false）时不动它。
      if (!focusInside) return;
      queueMicrotask(() => {
        const now = document.activeElement;
        if (!now || now === document.body) focusAnchor();
      });
    };
    // initialFocus / anchorRef 是 ref，开关项读 optionsRef，都不参与依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return close;
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
