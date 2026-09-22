import React from "react";
import { isImeComposing } from "./ws-dialog.jsx";

/* ==========================================================
   「排版与舒适度」快捷面板 + 它的几种表单控件
   原型期这里是一块写死浅色毛玻璃的浮窗（夜间主题下也是白的）、iOS 绿的开关、可拖动但只认鼠标、
   z-index 压在所有对话框之上，标题叫「Tweaks」。现在：
   · 外观全部来自 ws-shell.css 的纸 / 墨 token，昼夜都对；
   · role="dialog"（非模态），打开时焦点进面板，Esc 关闭，关闭后焦点回到打开它的按钮；
   · 固定在侧栏旁边（打开它的按钮就在侧栏底部），层级用 --z-panel；
   · 只保留真正在用的控件（分段单选、开关、滑杆、下拉），文本 / 数字 / 取色 / 按钮控件已删除。
   打开：派发窗口事件 ws:tweaks-open（侧栏底部按钮与命令面板都这么做）。
   偏好的读写与 schema 在 ws-prefs.js。纯 ESM，不写 window。
   ========================================================== */

const { useEffect, useRef, useState } = React;

function TweaksPanel({ title = "排版与舒适度", children }) {
  const [open, setOpen] = useState(false);
  const panelRef = useRef(null);
  const openerRef = useRef(null);
  const titleId = React.useId();

  useEffect(() => {
    const onOpen = () => {
      const active = typeof document !== "undefined" ? document.activeElement : null;
      if (!panelRef.current || !panelRef.current.contains(active)) openerRef.current = active;
      setOpen(true);
      // 已经开着时再点一次入口：把焦点送回面板
      if (panelRef.current) focusFirst(panelRef.current);
    };
    window.addEventListener("ws:tweaks-open", onOpen);
    return () => window.removeEventListener("ws:tweaks-open", onOpen);
  }, []);

  useEffect(() => {
    if (open && panelRef.current) focusFirst(panelRef.current);
  }, [open]);

  const close = () => {
    setOpen(false);
    const opener = openerRef.current;
    openerRef.current = null;
    if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus({ preventScroll: true });
  };

  const onKeyDown = (event) => {
    if (event.key !== "Escape" || isImeComposing(event)) return;
    event.preventDefault();
    event.stopPropagation();
    close();
  };

  if (!open) return null;
  return (
    <section ref={panelRef} className="twk-panel" role="dialog" aria-modal="false" aria-labelledby={titleId}
      tabIndex={-1} onKeyDown={onKeyDown}>
      <div className="twk-hd">
        <h2 className="twk-title" id={titleId}>{title}</h2>
        <button type="button" className="twk-x" aria-label={`关闭${title}`} onClick={close}>×</button>
      </div>
      <div className="twk-body">
        {children}
      </div>
    </section>
  );
}

/* 分段单选是 roving tabindex：没选中的段 tabindex=-1，焦点要落在选中的那段上（夜灯主题打开时是「夜灯」，
   不是排在第一的「白昼」），所以跳过 tabindex=-1 的控件。 */
function focusFirst(root) {
  const target = root.querySelector(
    ".twk-body button:not([disabled]):not([tabindex='-1']), .twk-body input:not([disabled]), .twk-body select:not([disabled])",
  ) || root;
  if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
}

// ── Layout helpers ──────────────────────────────────────────────────────────

function TweakSection({ label, children }) {
  return (
    <>
      <h3 className="twk-sect">{label}</h3>
      {children}
    </>
  );
}

function TweakRow({ label, value, hint, children, inline = false }) {
  return (
    <div className={inline ? "twk-row twk-row-h" : "twk-row"}>
      <div className="twk-lbl">
        <span>{label}</span>
        {value != null && <span className="twk-val">{value}</span>}
      </div>
      {children}
      {hint ? <p className="twk-hint">{hint}</p> : null}
    </div>
  );
}

// ── Controls ────────────────────────────────────────────────────────────────

function TweakSlider({ label, value, min = 0, max = 100, step = 1, unit = "", hint, onChange }) {
  const shown = `${value}${unit}`;
  return (
    <TweakRow label={label} value={shown} hint={hint}>
      <input type="range" className="twk-slider" min={min} max={max} step={step}
        value={value} aria-label={label} aria-valuetext={shown}
        onChange={(e) => onChange(Number(e.target.value))} />
    </TweakRow>
  );
}

function TweakToggle({ label, value, hint, onChange }) {
  return (
    <div className="twk-row">
      <div className="twk-row-h">
        <div className="twk-lbl"><span>{label}</span></div>
        <button type="button" className="twk-toggle" data-on={value ? "1" : "0"}
          role="switch" aria-checked={!!value} aria-label={label}
          onClick={() => onChange(!value)}><i /></button>
      </div>
      {hint ? <p className="twk-hint">{hint}</p> : null}
    </div>
  );
}

/* 分段单选：每段放得下时是一排按钮（单选组，方向键在组内移动），放不下时退成下拉框。
   面板正文约 268px 宽，13px 中文字约 13px 一个：两段各 ≤ 8 字、三段 ≤ 5 字、四段 ≤ 3 字。 */
const SEGMENT_FIT = { 2: 8, 3: 5, 4: 3 };

function TweakRadio({ label, value, options, hint, onChange }) {
  const opts = options.map((o) => (typeof o === "object" ? o : { value: o, label: String(o) }));
  const maxLen = opts.reduce((m, o) => Math.max(m, String(o.label).length), 0);
  if (!(maxLen <= (SEGMENT_FIT[opts.length] ?? 0))) {
    // <select> 只吐字符串——映射回原选项的值，保持数字 / 布尔类型不变
    const resolve = (s) => {
      const hit = opts.find((o) => String(o.value) === s);
      return hit ? hit.value : s;
    };
    return <TweakSelect label={label} value={value} options={opts} hint={hint} onChange={(s) => onChange(resolve(s))} />;
  }
  const idx = Math.max(0, opts.findIndex((o) => o.value === value));
  const n = opts.length;
  const onKeyDown = (event) => {
    const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    const group = event.currentTarget.parentElement;
    const radios = group ? [...group.querySelectorAll('[role="radio"]')] : [];
    // 从有焦点的那段数起，而不是从选中的那段：两者不一致时（例如点过别处又用 Tab 回来）方向键不会反着走
    const here = radios.indexOf(event.currentTarget);
    const from = here >= 0 ? here : idx;
    let next = null;
    if (event.key in keys) next = (from + keys[event.key] + n) % n;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = n - 1;
    if (next == null) return;
    event.preventDefault();
    onChange(opts[next].value);
    const target = radios[next];
    if (target) target.focus();
  };
  return (
    <TweakRow label={label} hint={hint}>
      <div role="radiogroup" aria-label={label} className="twk-seg">
        <div className="twk-seg-thumb" aria-hidden="true"
          style={{ left: `calc(2px + ${idx} * (100% - 4px) / ${n})`, width: `calc((100% - 4px) / ${n})` }} />
        {opts.map((o, i) => (
          <button key={String(o.value)} type="button" role="radio" aria-checked={o.value === value} aria-label={o.label}
            tabIndex={i === idx ? 0 : -1} onKeyDown={onKeyDown} onClick={() => onChange(o.value)}>
            {o.label}
          </button>
        ))}
      </div>
    </TweakRow>
  );
}

function TweakSelect({ label, value, options, hint, onChange }) {
  return (
    <TweakRow label={label} hint={hint}>
      <select className="twk-field" value={value} aria-label={label} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => {
          const v = typeof o === "object" ? o.value : o;
          const l = typeof o === "object" ? o.label : o;
          return <option key={String(v)} value={v}>{l}</option>;
        })}
      </select>
    </TweakRow>
  );
}

export {
  TweaksPanel, TweakSection, TweakRow,
  TweakSlider, TweakToggle, TweakRadio, TweakSelect,
};
