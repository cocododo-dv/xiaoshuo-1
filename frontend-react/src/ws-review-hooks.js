import React from "react";
import { isImeComposing } from "./lib/keyboard.js";
import { rvUnresolve } from "./ws-review-store.js";
import { rvNeedsChoice } from "./ws-review-parts.jsx";

/* 待办收件箱视图的两段行为（2026-09-29 从 WsReview 里拆出）：可撤销的处理批次、键盘操作。 */

export const RV_UNDO_MS = 6000;

/* 可撤销的处理：receipt(entries) 记下最近一批（{ item, index }）并给出带「撤销」的回执；
   undo(batch) 把这一批按原位置放回视图列表、服务端改回未处理、今日计数退回。 */
export function useRvUndo({ setItems, setDoneToday, notify, clearLocalToast }) {
  const undoRef = React.useRef(null);        // 最近一批可撤销的处理：{ entries, at }

  const undo = (batch) => {
    const entries = (batch && batch.entries) || [];
    if (!entries.length) return;
    if (undoRef.current === batch) undoRef.current = null;
    setItems(prev => {
      const next = prev.filter(x => !entries.some(e => e.item.id === x.id));
      entries.slice().sort((a, b) => a.index - b.index).forEach(e => {
        next.splice(Math.min(e.index, next.length), 0, e.item);
      });
      return next;
    });
    rvUnresolve(entries.map(e => e.item.id), entries.map(e => e.item));
    setDoneToday(n => Math.max(0, n - entries.length));
    clearLocalToast();
  };

  const receipt = (entries) => {
    const batch = { entries, at: Date.now() };
    undoRef.current = batch;
    const text = entries.length === 1 ? `已处理「${entries[0].item.title}」` : `已处理 ${entries.length} 条待办`;
    notify(text, { label: "撤销", onClick: () => undo(batch) });
  };

  return { undoRef, undo, receipt };
}

/* 键盘：J / K（或在卡片标题上用 ↑ ↓）在卡片间移动焦点；焦点在某张卡的标题上时
   E 处理、S 稍后；U 撤销刚才那一批。只接管这些键——回车 / 空格留给按钮本身，
   焦点在输入框、别的按钮组、对话框里或收件箱之外时一概不管。
   处理函数每次渲染换成最新的（读当前可见列表），监听只挂一次。 */
export function useRvKeyboard({ rootRef, visible, selId, setSelId, setKbd, setOpenId, undoRef, undo, resolve, snooze }) {
  const focusRow = (id) => {
    const root = rootRef.current;
    if (!root || !id) return;
    const row = root.querySelector(`.rv-item[data-id="${String(id).replace(/["\\]/g, "\\$&")}"] .rv-row`);
    if (row) row.focus({ preventScroll: false });
  };

  const onKeyRef = React.useRef(null);
  onKeyRef.current = (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isImeComposing(e)) return;
    const root = rootRef.current;
    const t = e.target;
    if (!root || !t) return;
    const onPage = root.contains(t);
    if (!onPage && t !== document.body && t !== document.documentElement) return;
    if (t.closest && t.closest('[role="dialog"]')) return;
    const tag = (t.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable) return;
    const onRow = !!(t.classList && t.classList.contains("rv-row"));
    const currentId = onRow ? (t.closest(".rv-item") || {}).getAttribute?.("data-id") : null;
    const key = e.key;

    if (key === "u" || key === "U") {
      const batch = undoRef.current;
      if (batch && Date.now() - batch.at < RV_UNDO_MS) { e.preventDefault(); setKbd(true); undo(batch); }
      return;
    }
    const down = key === "j" || (onRow && key === "ArrowDown");
    const up = key === "k" || (onRow && key === "ArrowUp");
    if (down || up) {
      if (!visible.length) return;
      e.preventDefault();
      setKbd(true);
      const ids = visible.map(x => x.id);
      const from = currentId ? ids.indexOf(currentId) : ids.indexOf(selId);
      const next = from < 0 ? (down ? 0 : ids.length - 1) : (from + (down ? 1 : -1) + ids.length) % ids.length;
      setSelId(ids[next]);
      focusRow(ids[next]);
      return;
    }
    if (onRow && currentId && (key === "e" || key === "s")) {
      e.preventDefault();
      setKbd(true);
      const cur = visible.find(x => x.id === currentId);
      if (!cur) return;
      if (key === "e" && rvNeedsChoice(cur)) { setOpenId(cur.id); return; } // 决策项：展开让你选，不默认划掉
      const idx = visible.indexOf(cur);
      const next = visible[idx + 1] || visible[idx - 1];
      if (key === "e") resolve(cur.id); else snooze(cur.id);
      if (next) { setSelId(next.id); window.setTimeout(() => focusRow(next.id), 320); }
    }
  };
  React.useEffect(() => {
    const onKey = (e) => onKeyRef.current && onKeyRef.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
