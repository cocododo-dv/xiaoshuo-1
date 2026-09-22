import React from "react";
import { isImeComposing, topModalLayer } from "./ws-dialog.jsx";

/* ==========================================================
   写作台的通用 hook（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   · useWrEvent(fn)：身份稳定、永远调到最新闭包的回调——传给 React.memo 的大纲 / 上下文栏，
     它们就不会因为父组件重渲染而跟着重渲染。
   · useWrCounter / useWrCount：本场字数的小外部仓库。字数每敲一个字都在变，
     过去存在 WriterRoom 的 state 里，整棵树（大纲的每一章每一场、上下文栏、设计卡）
     跟着每个按键重渲染；现在只有读字数的几个叶子组件订阅它。
   · useWrLayout：书桌布局里两侧栏停靠 / 叠放的规则与开合。
   · useRailResize：拖动两侧栏的分隔线，宽度记在 wr-rail。
   · useImmersionChrome：沉浸写作时，外框在鼠标 / 键盘静止一会儿后淡出。
   · useWriterShortcuts：写作台的全局快捷键（⌘/Ctrl+J、.、1、2 与 Esc）。
   ESM 模块，不写 window。
   ========================================================== */

const { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } = React;

export function useWrEvent(fn) {
  const ref = useRef(fn);
  useLayoutEffect(() => { ref.current = fn; });
  return useCallback((...args) => ref.current(...args), []);
}

/* 收起的抽屉 / 托盘只是移出了画面，DOM 还在：标成 inert，Tab 键就不会走进看不见的按钮里。
   （React 18 还不认 inert 这个属性，所以用副作用直接设。） */
export function useWrInert(ref, inert) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (inert) el.setAttribute("inert", ""); else el.removeAttribute("inert");
  }, [ref, inert]);
}

/* ---------------- 字数 ---------------- */

export function useWrCounter() {
  const [counter] = useState(() => {
    let value = 0;
    const subs = new Set();
    return {
      get: () => value,
      set(next) {
        if (next === value) return;
        value = next;
        subs.forEach((fn) => fn());
      },
      subscribe(fn) {
        subs.add(fn);
        return () => subs.delete(fn);
      },
    };
  });
  return counter;
}

export function useWrCount(counter) {
  return useSyncExternalStore(counter.subscribe, counter.get, counter.get);
}

/* ---------------- 断点与停靠 ---------------- */

function wrMatches(query) {
  try { return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia(query).matches; } catch (e) { return false; }
}
/* 断点一律写成 max-width：单测里的 matchMedia 桩对任何查询都回 false，表示「宽屏」 */
const WR_MQ_NARROW = "(max-width: 999px)";   // 大纲也不再停靠
const WR_MQ_MID = "(max-width: 1279px)";     // 上下文栏改为叠在稿纸上的抽屉

function useWrMedia(query) {
  const [hit, setHit] = useState(() => wrMatches(query));
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return undefined;
    const mq = window.matchMedia(query);
    const on = () => setHit(!!mq.matches);
    on();
    if (mq.addEventListener) mq.addEventListener("change", on); else if (mq.addListener) mq.addListener(on);
    return () => { if (mq.removeEventListener) mq.removeEventListener("change", on); else if (mq.removeListener) mq.removeListener(on); };
  }, [query]);
  return hit;
}

/* 停靠规则（共享断点：1280 以下右栏改为抽屉）：书桌布局下，大纲在 1000 以上停靠，
   上下文在 1280 以上停靠；停靠的栏与稿纸并排、默认打开，叠放的栏召之即来、点遮罩即走。
   深改姿态例外：诊断栏要和它诊断的正文并排看（点正文高亮、选一项滚到那一句），
   叠成抽屉就会被遮罩盖住正文——所以深改时右栏在 1000 以上也停靠。 */
export function useWrLayout(isDesk, posture = "draft") {
  const narrow = useWrMedia(WR_MQ_NARROW);
  const mid = useWrMedia(WR_MQ_MID);
  const dockLeft = isDesk && !narrow;
  const dockRight = isDesk && (!mid || (posture === "deep" && !narrow));
  const [leftOpen, setLeftOpen] = useState(() => isDesk && !wrMatches(WR_MQ_NARROW));
  const [rightOpen, setRightOpen] = useState(() => isDesk && !wrMatches(WR_MQ_MID));

  // 停靠的栏默认打开，叠放的栏默认收起；窗口跨过断点（或深改让右栏改停靠）时重新按这个规则摆。
  // 两侧分开记：进出深改只动右栏，作者收起的大纲不会被顺手又打开
  useEffect(() => { setLeftOpen(dockLeft); }, [dockLeft]);
  useEffect(() => { setRightOpen(dockRight); }, [dockRight]);

  /* 打开一侧时，另一侧若是叠放抽屉就收起（遮罩会盖住它）；停靠的栏从不被顺手收起 */
  const openLeft = useWrEvent(() => { setLeftOpen(true); if (!dockRight) setRightOpen(false); });
  const openRight = useWrEvent(() => { setRightOpen(true); if (!dockLeft) setLeftOpen(false); });
  const toggleLeft = useWrEvent(() => { if (leftOpen) setLeftOpen(false); else openLeft(); });
  const toggleRight = useWrEvent(() => { if (rightOpen) setRightOpen(false); else openRight(); });
  const closeLeft = useCallback(() => setLeftOpen(false), []);
  const closeRight = useCallback(() => setRightOpen(false), []);
  /* 只收叠放的抽屉：Esc、点遮罩、从别的台子跳进来，都不碰停靠的栏 */
  const closeOverlayRails = useWrEvent(() => {
    if (!dockLeft) setLeftOpen(false);
    if (!dockRight) setRightOpen(false);
  });
  const overlayOpen = (leftOpen && !dockLeft) || (rightOpen && !dockRight);
  return {
    dockLeft, dockRight, leftOpen, rightOpen, overlayOpen,
    openLeft, openRight, toggleLeft, toggleRight, closeLeft, closeRight, closeOverlayRails,
  };
}

/* ---------------- 侧栏宽度 ---------------- */

/* 两侧栏的默认宽度：大纲 240、上下文 272（在 1280 宽的屏上两栏都停靠时，稿纸仍有近 30 字一行）；
   作者拖过的宽度记在 wr-rail 里 */
export const WR_RAIL_DEFAULT = { l: 240, r: 272 };
const RAIL_MIN = 200;
const RAIL_MAX = 360;

function wrLoadRail() {
  try { const s = JSON.parse(localStorage.getItem("wr-rail")); if (s && s.l && s.r) return { l: s.l, r: s.r }; } catch (e) {}
  return { ...WR_RAIL_DEFAULT };
}

export function useRailResize() {
  const [rail, setRail] = useState(wrLoadRail);
  const [dragging, setDragging] = useState(false);
  const stopRef = useRef(null);

  const startRail = useWrEvent((side, event) => {
    event.preventDefault();
    if (stopRef.current) stopRef.current();
    const origin = { x: event.clientX, l: rail.l, r: rail.r };
    const clamp = (value) => Math.max(RAIL_MIN, Math.min(RAIL_MAX, value));
    const move = (e) => {
      const dx = e.clientX - origin.x;
      setRail((prev) => (side === "l" ? { ...prev, l: clamp(origin.l + dx) } : { ...prev, r: clamp(origin.r - dx) }));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      stopRef.current = null;
      setDragging(false);
    };
    stopRef.current = stop;
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    setDragging(true);
  });
  const resetRail = useCallback((side) => setRail((prev) => ({ ...prev, [side]: WR_RAIL_DEFAULT[side] })), []);

  // 拖动中不写存储，松手再记
  useEffect(() => {
    if (dragging) return;
    try { localStorage.setItem("wr-rail", JSON.stringify({ l: rail.l, r: rail.r })); } catch (e) {}
  }, [rail, dragging]);
  useEffect(() => () => { if (stopRef.current) stopRef.current(); }, []);

  return { railL: rail.l, railR: rail.r, dragging, startRail, resetRail };
}

/* ---------------- 沉浸 ---------------- */

export function useImmersionChrome(immersion) {
  const [chrome, setChrome] = useState(true);
  useEffect(() => {
    if (!immersion) { setChrome(true); return undefined; }
    let timer = null;
    const reveal = () => {
      setChrome(true);
      clearTimeout(timer);
      timer = setTimeout(() => setChrome(false), 2600);
    };
    reveal();
    window.addEventListener("mousemove", reveal);
    window.addEventListener("keydown", reveal);
    return () => {
      window.removeEventListener("mousemove", reveal);
      window.removeEventListener("keydown", reveal);
      clearTimeout(timer);
    };
  }, [immersion]);
  return chrome;
}

/* ---------------- 快捷键 ---------------- */

/* handlers: { onAI, onImmersion, onToggleLeft, onToggleRight, onEscape }，每次渲染传最新的即可。
   · 输入法组字中的按键一律不算（组字时按 Esc 只是撤掉拼音：过去它会退出沉浸、收起托盘）。
   · 模态层开着时键只作用于最上层：命令面板、确认框、作品切换开着时一个快捷键都不认
     （过去 ⌘J 在命令面板底下打开托盘、把焦点从面板里拽走，⌘1 在面板背后收起大纲）；
     写作台自己的续写托盘在最上层时只认 Esc——由 onEscape 关掉它。 */
export function useWriterShortcuts(handlers) {
  const ref = useRef(handlers);
  useLayoutEffect(() => { ref.current = handlers; });
  useEffect(() => {
    const onKey = (e) => {
      if (isImeComposing(e)) return;
      const h = ref.current;
      const meta = e.metaKey || e.ctrlKey;
      const key = String(e.key || "");
      const top = topModalLayer();
      if (top && !(key === "Escape" && top.classList && top.classList.contains("wr-tray"))) return;
      if (meta && key.toLowerCase() === "j") { e.preventDefault(); h.onAI(); }
      else if (meta && key === ".") { e.preventDefault(); h.onImmersion(); }
      else if (meta && key === "1") { e.preventDefault(); h.onToggleLeft(); }
      else if (meta && key === "2") { e.preventDefault(); h.onToggleRight(); }
      else if (key === "Escape") {
        /* 这一下 Esc 已经被别处用掉了（@ 档案选择器、选区工具条、弹层、对话框）就不再收栏 */
        if (e.defaultPrevented) return;
        if (document.querySelector(".wr-irw-bar, .wr-irw-pop, .wr-mention, .ws-dialog-scrim")) return;
        h.onEscape();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
