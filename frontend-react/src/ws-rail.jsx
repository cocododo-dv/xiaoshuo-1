import React from "react";
import { I } from "./icons.jsx";
import { useReviewBadge } from "./ws-review-badge.jsx";
import { ViewErrorBoundary } from "./ws-view-boundary.jsx";
import { WorkSwitcher } from "./ws-work-switcher.jsx";
import { lazyNamed } from "./ws-lazy.jsx";
import { navGroupsForMode, systemNavGroup } from "./ws-nav.js";
import { modShortcut } from "./lib/platform.js";

/* ==========================================================
   Rail — 左侧导航
   · 折叠 64px 只见图标；指针停留约 150ms（悬停意图）或用键盘把焦点移进来时展开，不挤占内容。
     展开态由这里算好写成 .is-expanded：鼠标点过的按钮会留着焦点，对话框关掉后焦点也会被送回
     侧栏里的入口——这两种都不该让侧栏展开、页面变暗，所以只认「这次焦点是用 Tab 或侧栏里的方向键
     挪过来的」；在侧栏里按回车 / 空格用掉一个入口后也收回。
   · 分组标题在折叠时只是一条细线，展开时文字出现在同一行里——高度不变，悬停时条目不会跳位。
   · 底部常驻：设置 / 回收站、同步与恢复、昼夜、排版与舒适度、作家 / 高级模式切换。
   纯 ESM，不写 window。
   ========================================================== */

const { useEffect, useRef, useState } = React;

const RAIL_HOVER_INTENT_MS = 150;

/* 同步与恢复中心连同恢复稿存储一起懒加载，不进首屏 */
const LazyWrRecoveryCenter = lazyNamed(() => import("./wr-recovery-center.jsx"), "WrRecoveryCenter");

/* 最近一次操作是不是「用键盘把焦点挪进 / 挪过侧栏」（整个文档共用一份）。
   只有 Tab，或者焦点本来就在侧栏里时的方向键 / Home / End 才算；其余任何键（Esc / 回车 / 空格 / 打字）
   都把它清掉。所以对话框、命令面板、作品弹层里按过的方向键不算，在里面按过的 Tab 也会被随后关掉浮层的
   Esc / 回车清掉——否则浮层关掉、把焦点送回侧栏入口时，侧栏会当成键盘进入而展开、压暗页面
   （从命令面板用 ↓ + 回车跳页就是这样）。
   Shift 先于 Tab 到达，清掉后紧接着的 Tab 又会记上，所以 Shift+Tab 照常。
   监听挂在 window 的捕获阶段，比 document 上的对话框 Esc 处理更早，谁 stopPropagation 都拦不住。 */
let railLastInput = "pointer";
if (typeof window !== "undefined") {
  window.addEventListener("keydown", (e) => {
    // 浏览器自动填充派发的 keydown 可能没有 key，按空串处理
    const key = e.key || "";
    const inRail = Boolean(e.target && typeof e.target.closest === "function" && e.target.closest(".ws-rail"));
    const moves = key === "Tab" || (inRail && (key.startsWith("Arrow") || key === "Home" || key === "End"));
    railLastInput = moves ? "keyboard" : "other";
  }, true);
  window.addEventListener("pointerdown", () => { railLastInput = "pointer"; }, true);
  window.addEventListener("mousedown", () => { railLastInput = "pointer"; }, true);
}

/* 展开态：悬停意图（原生 pointerenter / pointerleave——React 的合成 enter/leave 按组件树算，
   从侧栏 portal 出去的对话框也算「在侧栏里」）或键盘焦点在侧栏里。 */
function useRailExpansion(railRef) {
  const [hover, setHover] = useState(false);
  const [keyboard, setKeyboard] = useState(false);
  const intentRef = useRef(null);

  useEffect(() => {
    const rail = railRef.current;
    if (!rail) return undefined;
    const onEnter = (e) => {
      if (e.pointerType === "touch") return;
      window.clearTimeout(intentRef.current);
      intentRef.current = window.setTimeout(() => setHover(true), RAIL_HOVER_INTENT_MS);
    };
    const onLeave = () => {
      window.clearTimeout(intentRef.current);
      intentRef.current = null;
      setHover(false);
    };
    const onFocusIn = () => { setKeyboard(railLastInput === "keyboard"); };
    const onFocusOut = (e) => { if (!rail.contains(e.relatedTarget)) setKeyboard(false); };
    // 指针在侧栏里点了东西：交给悬停意图管，键盘展开态让位
    const onPointerDown = () => setKeyboard(false);
    // 用回车 / 空格在侧栏里按下一个入口（跳页、切昼夜）：作者要看的是结果，侧栏收回去；
    // 焦点仍在那个入口上，再按 Tab / 方向键就又展开
    const onKeyDown = (e) => { if (e.key === "Enter" || e.key === " ") setKeyboard(false); };
    rail.addEventListener("pointerenter", onEnter);
    rail.addEventListener("pointerleave", onLeave);
    rail.addEventListener("focusin", onFocusIn);
    rail.addEventListener("focusout", onFocusOut);
    rail.addEventListener("pointerdown", onPointerDown);
    rail.addEventListener("keydown", onKeyDown);
    return () => {
      rail.removeEventListener("pointerenter", onEnter);
      rail.removeEventListener("pointerleave", onLeave);
      rail.removeEventListener("focusin", onFocusIn);
      rail.removeEventListener("focusout", onFocusOut);
      rail.removeEventListener("pointerdown", onPointerDown);
      rail.removeEventListener("keydown", onKeyDown);
      window.clearTimeout(intentRef.current);
    };
  }, [railRef]);

  return hover || keyboard;
}

function RailItem({ item, active, badge, go }) {
  const Ic = I[item.icon] || I.Dot;
  return (
    <button type="button" className={`ws-item ${active ? "is-active" : ""}`} onClick={() => go(item.id)}
      aria-current={active ? "page" : undefined}
      aria-label={badge ? `${item.label}，${badge} 条紧急待办` : undefined}>
      <span className="ws-item-ic"><Ic size={19} /></span>
      <span className="ws-item-label">{item.label}</span>
      {badge && <span className="ws-item-badge" aria-hidden="true">{badge}</span>}
    </button>
  );
}

function RailGroup({ group, view, go, badge, divider }) {
  return (
    <div className="ws-nav-group" role="group" aria-label={group.label}>
      {divider && <div className="ws-nav-label" aria-hidden="true"><span>{group.label}</span></div>}
      {group.items.map(n => (
        <RailItem key={n.id} item={n} active={view === n.id} go={go} badge={n.liveBadge ? badge : null} />
      ))}
    </div>
  );
}

const WS_MODES = [
  { id: "writer", label: "作家", hint: "作家模式：只显示日常写作" },
  { id: "advanced", label: "高级", hint: "高级模式：再显示生产、质控与运维工具" },
];

function ModeSwitch({ mode, setTweak }) {
  const onKeyDown = (e) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    let next = mode === "writer" ? "advanced" : "writer";
    if (e.key === "Home") next = "writer";
    if (e.key === "End") next = "advanced";
    setTweak("mode", next);
    const target = e.currentTarget.parentElement && e.currentTarget.parentElement.querySelector(`[data-mode="${next}"]`);
    if (target) target.focus();
  };
  return (
    <div className="ws-mode-switch" role="radiogroup" aria-label="界面模式">
      {WS_MODES.map(m => {
        const on = mode === m.id;
        return (
          <button key={m.id} type="button" role="radio" aria-checked={on} tabIndex={on ? 0 : -1} data-mode={m.id}
            className={`ws-mode-btn ${on ? "is-active" : ""}`} title={m.hint}
            onClick={() => setTweak("mode", m.id)} onKeyDown={onKeyDown}>
            {m.label}
          </button>
        );
      })}
    </div>
  );
}

/* 同步与恢复中心的模块还在加载时，先占住同样大小的位置，免得底部在首屏后跳一下。 */
function RecoveryPlaceholder() {
  return (
    <span className="ws-foot-btn wrr-trigger is-pending" aria-hidden="true">
      <span className="ws-item-ic"><I.Save size={18} /></span>
      <span className="ws-foot-label">同步与恢复</span>
    </span>
  );
}

/* 导航区装不下时（高级模式 + 矮窗口）上下边缘淡出，提示还能滚动 */
function useScrollFade(navRef, deps) {
  const [fade, setFade] = useState("");
  useEffect(() => {
    const el = navRef.current;
    if (!el) return undefined;
    const update = () => {
      const top = el.scrollTop > 2;
      const bottom = el.scrollTop + el.clientHeight < el.scrollHeight - 2;
      setFade(top && bottom ? "both" : (top ? "top" : (bottom ? "bottom" : "")));
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null;
    if (observer) observer.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
      if (observer) observer.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return fade;
}

function Rail({ view, go, t, setTweak, mode, onPalette }) {
  const groups = navGroupsForMode(mode);
  const system = systemNavGroup();
  const reviewBadge = useReviewBadge();  // 订阅式：处理完即消失，按作品隔离
  const railRef = useRef(null);
  const navRef = useRef(null);
  const expanded = useRailExpansion(railRef);
  const fade = useScrollFade(navRef, [mode]);
  const night = t.theme === "night";

  // 当前页面的条目若被滚出视野，滚回来
  useEffect(() => {
    const el = navRef.current;
    const current = el && el.querySelector('[aria-current="page"]');
    if (current && typeof current.scrollIntoView === "function") current.scrollIntoView({ block: "nearest" });
  }, [view, mode]);

  return (
    <aside ref={railRef} className={`ws-rail ${expanded ? "is-expanded" : ""}`} aria-label="侧栏">
      <WorkSwitcher go={go} />

      <button type="button" className="ws-cmdk" onClick={onPalette} title={`快速跳转（${modShortcut("K")}）`}>
        <span className="ws-item-ic"><I.Search size={18} /></span>
        <span className="ws-cmdk-label">快速跳转…</span>
        <kbd>{modShortcut("K")}</kbd>
      </button>

      <nav ref={navRef} className="ws-nav ws-nav-scroll" aria-label="页面" data-fade={fade || undefined}>
        {groups.map((g, i) => (
          <RailGroup key={g.id} group={g} view={view} go={go} badge={reviewBadge} divider={i > 0} />
        ))}
      </nav>

      <div className="ws-rail-foot">
        {system && <RailGroup group={system} view={view} go={go} badge={null} divider />}
        <div className="ws-foot-tools">
          <ViewErrorBoundary silent resetKey="recovery-center">
            <React.Suspense fallback={<RecoveryPlaceholder />}><LazyWrRecoveryCenter /></React.Suspense>
          </ViewErrorBoundary>
          <button type="button" className="ws-foot-btn" onClick={() => setTweak("theme", night ? "day" : "night")} title="切换昼夜">
            <span className="ws-item-ic">{night ? <I.Sun size={18} /> : <I.Moon size={18} />}</span>
            <span className="ws-foot-label">{night ? "切到白昼" : "切到夜灯"}</span>
          </button>
          <button type="button" className="ws-foot-btn" onClick={() => window.dispatchEvent(new CustomEvent("ws:tweaks-open"))} title="舒适度设置">
            <span className="ws-item-ic"><I.Sliders size={18} /></span>
            <span className="ws-foot-label">排版与舒适度</span>
          </button>
        </div>
        <ModeSwitch mode={mode} setTweak={setTweak} />
      </div>
    </aside>
  );
}

export { Rail };
