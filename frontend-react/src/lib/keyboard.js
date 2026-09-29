/* 键盘小工具（2026-09-29 前端共享层）：方向键在一组控件里循环移动的下标、输入法组字判定、
   WAI-ARIA tabs 的通用键盘行为。纯函数 / 纯 DOM，不 import 视图，不写 window。
   ws-dialog.jsx（isImeComposing）与 a11y-tabs.js（onRovingTabKeyDown）照旧转出这里的实现。 */

/* 漫游下标：←↑ 往前、→↓ 往后（首尾循环），Home / End 跳首尾；别的键返回 null。
   at 是当前项在 n 项里的位置（-1 = 不在组里：→↓ 落到第一项，←↑ 从末尾往回数）。 */
export function rovingIndex(key, at, n) {
  if (key === "Home") return 0;
  if (key === "End") return n - 1;
  if (key === "ArrowLeft" || key === "ArrowUp") return (at - 1 + n) % n;
  if (key === "ArrowRight" || key === "ArrowDown") return (at + 1) % n;
  return null;
}

/* rovingIndex 认的六个键 */
export const ROVING_KEYS = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];

/* 输入法组字中的回车 / 方向键不是命令（拼音选词时按回车会误触发提交）。 */
export function isImeComposing(event) {
  if (!event) return false;
  const native = event.nativeEvent || event;
  return Boolean(native.isComposing || event.isComposing || native.keyCode === 229 || event.keyCode === 229);
}

/**
 * WAI-ARIA tabs 的通用键盘行为：方向键移动并激活，Home/End 跳首尾。
 * 组件只需维护 aria-selected/tabIndex；这里按当前 tablist 的真实 DOM 顺序工作。
 */
export function onRovingTabKeyDown(event) {
  if (!ROVING_KEYS.includes(event.key)) return;
  const list = event.currentTarget && event.currentTarget.closest('[role="tablist"]');
  if (!list) return;
  const tabs = Array.from(list.querySelectorAll('[role="tab"]')).filter((tab) => !tab.disabled);
  if (!tabs.length) return;
  const current = Math.max(0, tabs.indexOf(event.currentTarget));
  const next = rovingIndex(event.key, current, tabs.length);
  event.preventDefault();
  tabs[next].focus();
  tabs[next].click();
}
