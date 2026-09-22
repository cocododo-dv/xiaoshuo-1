import React from "react";
import { I } from "./icons.jsx";
import { onRovingTabKeyDown } from "./a11y-tabs.js";

/* 共享界面原语（2026-09-21 前端重构）。样式在 ws-ui.css。
   审计时同一个概念在各视图里各写一遍：6 种页头、13 种分段 / 页签、37 种标签、
   24 种空态、十几种提示条。新代码和改到的地方用这里的组件；类名是契约，
   测试 / 冒烟脚本若需要钩子，用 testId / className 透传。纯 ESM，不写 window。 */

const cx = (...parts) => parts.filter(Boolean).join(" ");

/* 页头：标题（衬线）+ 一句说明 + 右侧动作。crumb 只在确实能帮作者定位时才给
   （例如「第 3 章 · 第 2 场」），不要再在每个标题上面放装饰性小字。 */
export function PageHeader({ title, description, crumb, meta, actions, className, titleAs: TitleTag = "h1", testId, children }) {
  return (
    <header className={cx("ws-page-head", className)} data-testid={testId}>
      <div className="ws-page-head-main">
        {crumb ? <div className="ws-page-crumb">{crumb}</div> : null}
        <TitleTag className="ws-page-title">{title}</TitleTag>
        {description ? <p className="ws-page-desc">{description}</p> : null}
        {meta ? <div className="ws-page-meta">{meta}</div> : null}
        {children}
      </div>
      {actions ? <div className="ws-page-actions">{actions}</div> : null}
    </header>
  );
}

/* 单选组（role=radiogroup）的键盘约定：←↑ / →↓ 循环移动并选中，Home / End 跳首尾，焦点跟着选中走。
   Segmented 与视图里自绘的卡片式单选组（例如风格注入策略）共用；items: [{ value, disabled? }]，
   按钮按 items 的顺序排在 event.currentTarget（radiogroup）里。 */
export function onRadioGroupKeyDown(event, { items, value, onChange }) {
  const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
  if (!keys.includes(event.key)) return;
  const enabled = items.filter((item) => !item.disabled);
  if (!enabled.length) return;
  const at = Math.max(0, enabled.findIndex((item) => item.value === value));
  let next = at;
  if (event.key === "Home") next = 0;
  else if (event.key === "End") next = enabled.length - 1;
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (at - 1 + enabled.length) % enabled.length;
  else next = (at + 1) % enabled.length;
  event.preventDefault();
  if (onChange) onChange(enabled[next].value);
  const buttons = event.currentTarget.querySelectorAll('[role="radio"]:not([disabled])');
  if (buttons[next]) buttons[next].focus();
}

/* 漫游 tabindex：选中的那一项可以 Tab 到、其余 -1；没有选中（或选中的那项被禁用）时第一个可用项可以 Tab 到——
   否则整组一个 Tab 停靠点都没有。 */
export function radioTabIndex(items, value, itemValue) {
  const checked = items.find((item) => item.value === value && !item.disabled);
  const anchor = checked || items.find((item) => !item.disabled);
  return anchor && anchor.value === itemValue ? 0 : -1;
}

/* 分段单选：role=radiogroup，方向键在选项间移动并选中，Home/End 跳首尾。
   options: [{ value, label, count?, disabled?, title?, testId?, className? }]；
   每个选项都带 data-value，测试 / 冒烟脚本按值取选项，不依赖视图私有类名。
   busy：选中项引发的加载还没回来——只标 aria-busy，不禁用选项：方向键刚把焦点移到的那一项若随即被禁用，
   焦点会掉到 body，键盘用户就不在这组单选里了（分章面板的「怎么分」就是这样）。 */
export function Segmented({ value, options, onChange, label, size, block = false, busy = false, className, testId }) {
  const onKeyDown = (event) => onRadioGroupKeyDown(event, { items: options, value, onChange });
  return (
    <div role="radiogroup" aria-label={label} aria-busy={busy || undefined} className={cx("seg", block && "seg-block", className)} onKeyDown={onKeyDown} data-testid={testId}>
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={String(option.value)}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={radioTabIndex(options, value, option.value)}
            disabled={option.disabled}
            title={option.title}
            data-testid={option.testId}
            data-value={String(option.value)}
            className={cx("seg-btn", size === "sm" && "seg-btn-sm", on && "is-active", option.className)}
            onClick={() => { if (!on && onChange) onChange(option.value); }}
          >
            {option.icon || null}
            <span>{option.label}</span>
            {option.count != null ? <span className="seg-count">{option.count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

/* 下划线页签：role=tablist / tab，漫游焦点。tabs: [{ id, label, count?, disabled?, testId? }]。
   面板由调用方渲染；给 idPrefix 时生成 id = `${idPrefix}-tab-${id}`，选中的那一页签带
   aria-controls = `${idPrefix}-panel-${id}`——调用方多半只渲染选中的面板，给没渲染的面板写 aria-controls 是悬空引用。 */
export function Tabs({ value, tabs, onChange, label, idPrefix, className, testId }) {
  return (
    <div role="tablist" aria-label={label} className={cx("ws-tabs", className)} data-testid={testId}>
      {tabs.map((tab) => {
        const on = tab.id === value;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={idPrefix ? `${idPrefix}-tab-${tab.id}` : undefined}
            aria-controls={idPrefix && on ? `${idPrefix}-panel-${tab.id}` : undefined}
            aria-selected={on}
            tabIndex={on ? 0 : -1}
            disabled={tab.disabled}
            data-testid={tab.testId}
            className={cx("ws-tab", on && "is-active")}
            onKeyDown={onRovingTabKeyDown}
            onClick={() => { if (!on && onChange) onChange(tab.id); }}
          >
            {tab.icon || null}
            <span>{tab.label}</span>
            {tab.count != null ? <span className="ws-tab-count">{tab.count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

/* 状态 / 分类标签。tone: accent | ok | warn | danger | info | neutral。 */
export function Tag({ tone = "neutral", dot = false, outline = false, title, className, testId, children }) {
  return (
    <span className={cx("ws-tag", outline && "is-outline", className)} data-tone={tone} title={title} data-testid={testId}>
      {dot ? <span className="ws-tag-dot" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

const NOTICE_ICON = { info: "Info", ok: "CheckCircle", warn: "AlertTriangle", danger: "AlertTriangle", accent: "Info", neutral: "Info" };

/* 提示条：说清发生了什么、作者该做什么。错误用 role=alert，其余 role=status。 */
export function Notice({ tone = "info", title, children, actions, icon, role, className, testId }) {
  const Icon = icon === false ? null : (icon || I[NOTICE_ICON[tone]] || I.Info);
  return (
    <div className={cx("ws-notice", className)} data-tone={tone} role={role || (tone === "danger" ? "alert" : "status")} data-testid={testId}>
      {Icon ? <span className="ws-notice-icon" aria-hidden="true"><Icon size={15} /></span> : null}
      <div className="ws-notice-body">
        {title ? <div className="ws-notice-title">{title}</div> : null}
        {children ? <div className="ws-notice-text">{children}</div> : null}
      </div>
      {actions ? <div className="ws-notice-actions">{actions}</div> : null}
    </div>
  );
}

/* 空态：空页面是请作者动手的地方——标题说现状，正文说下一步，actions 放那个动作。 */
export function EmptyState({ icon, title, children, actions, compact = false, className, testId }) {
  const Icon = icon ? (typeof icon === "string" ? I[icon] : icon) : null;
  return (
    <div className={cx("ws-empty", compact && "is-compact", className)} data-testid={testId}>
      {Icon ? <span className="ws-empty-icon" aria-hidden="true"><Icon size={compact ? 18 : 24} /></span> : null}
      {title ? <div className="ws-empty-title">{title}</div> : null}
      {children ? <div className="ws-empty-text">{children}</div> : null}
      {actions ? <div className="ws-empty-actions">{actions}</div> : null}
    </div>
  );
}

/* 统计块：一个数、一个标签，可选单位与一句注释。 */
export function StatTile({ label, value, unit, hint, tone, className, testId }) {
  return (
    <div className={cx("ws-stat", className)} data-tone={tone} data-testid={testId}>
      <span className="ws-stat-label">{label}</span>
      <span className="ws-stat-value">{value}{unit ? <span className="ws-stat-unit">{unit}</span> : null}</span>
      {hint ? <span className="ws-stat-hint">{hint}</span> : null}
    </div>
  );
}

/* 区块小标题：图标 + 名称（不用大写字母间距），右侧可放计数或动作。 */
export function SectionLabel({ icon, children, aside, as: Tag_ = "div", className }) {
  const Icon = icon ? (typeof icon === "string" ? I[icon] : icon) : null;
  return (
    <Tag_ className={cx("ws-section-label", className)}>
      {Icon ? <span className="ws-section-label-icon" aria-hidden="true"><Icon size={14} /></span> : null}
      <span>{children}</span>
      {aside != null ? <span className="ws-section-label-aside">{aside}</span> : null}
    </Tag_>
  );
}

export function Spinner({ size = 14, label, className }) {
  return (
    <span className={cx("ws-spinner", className)} style={{ "--ws-spinner-size": `${size}px` }} role={label ? "status" : undefined} aria-label={label} aria-hidden={label ? undefined : "true"} />
  );
}

/* 方形图标按钮：label 必填（屏幕阅读器与悬停提示都用它）。 */
export function IconButton({ icon, label, title, onClick, size = "sm", variant = "quiet", disabled, pressed, className, testId, type = "button" }) {
  const Icon = typeof icon === "string" ? I[icon] : icon;
  const px = size === "xs" ? 13 : size === "lg" ? 18 : 15;
  return (
    <button
      type={type}
      className={cx("btn", `btn-${variant}`, `btn-${size}`, "btn-icon", pressed && "is-on", className)}
      aria-label={label}
      title={title || label}
      aria-pressed={pressed == null ? undefined : pressed}
      disabled={disabled}
      onClick={onClick}
      data-testid={testId}
    >
      {Icon ? <Icon size={px} /> : null}
    </button>
  );
}

export function CloseButton({ label = "关闭", title, onClick, disabled, className, testId, size = "sm" }) {
  return <IconButton icon="X" label={label} title={title} onClick={onClick} disabled={disabled} size={size} className={className} testId={testId} />;
}
