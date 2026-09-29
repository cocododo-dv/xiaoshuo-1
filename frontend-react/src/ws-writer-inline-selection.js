import React from "react";
import { MANUSCRIPT_BLOCK_SELECTOR } from "./manuscript-html.js";
import { wrSameRange } from "./ws-writer-inline-parts.jsx";
import { wrBlockSlice } from "./ws-writer-manuscript.js";

/* ==========================================================
   选区工具条 · 正文选区（2026-09-30 从 ws-writer-inline.jsx 拆出，审计 F03-18）
   ----------------------------------------------------------
   useEditorSelection({ editorRef, popRef, barRef })：记住工具条弹出时的那段选区，供改写 / 批注 / 深改交接用。
   · capture()：从当前选区取位置（选区不在正文里、或不到两个字时返回 null）；记下选中的字、Range、
     起始段里的那一截（深改 → 起草时按它重新选中）与结束段序号（「AI 看这几段」按范围看）。
   · intact(range)：那段字还在原处、没被改过、没塌成空的（整段重载后 Range 会塌到编辑器开头）。
   · returnFocus(caret)：焦点在工具条 / 弹层里（或随它们消失掉到 <body> 上）时还给正文——动过正文就把光标
     放到 caret，没动过就把原来的选区原样还回去；还回去的选区记成「安静」的，不再弹工具条（作者刚按过 Esc）。
   · quietMatches(sel)：当前选区是不是那段安静的选区（不是就忘掉它）；wake()：Alt+F10 时忘掉它；forget()：换场时全忘掉。
   ESM 模块，不写 window。
   ========================================================== */

const { useRef } = React;

export function useEditorSelection({ editorRef, popRef, barRef }) {
  const rangeRef = useRef(null);
  const textRef = useRef("");       // sel.toString()：送去改写的字
  const rangeTextRef = useRef("");  // Range.toString()：替换前据此确认那段字还在原处、没被改过
  const blockRef = useRef(null);    // 起始段里的那一截 { pid, pidEnd, start, end, text }
  const quietRef = useRef(null);

  const capture = () => {
    const ed = editorRef.current;
    const sel = window.getSelection();
    if (!ed || !sel || sel.isCollapsed || sel.rangeCount === 0) return null;
    const range = sel.getRangeAt(0);
    if (!ed.contains(range.commonAncestorContainer)) return null;
    const text = sel.toString();
    if (text.trim().length < 2) return null;
    rangeRef.current = range.cloneRange();
    textRef.current = text;
    rangeTextRef.current = range.toString();
    let block = range.startContainer;
    while (block && block.parentNode !== ed) block = block.parentNode;
    const blocks = Array.from(ed.querySelectorAll(MANUSCRIPT_BLOCK_SELECTOR));
    const pid = block ? blocks.indexOf(block) : -1;
    /* 选区跨了几段：记下结束段的序号（深改的「AI 看这几段」按范围看；改写仍只取起始段里的那一截） */
    let endBlock = range.endContainer;
    while (endBlock && endBlock.parentNode !== ed) endBlock = endBlock.parentNode;
    let pidEnd = endBlock ? blocks.indexOf(endBlock) : pid;
    if (pidEnd > pid && range.endOffset === 0) pidEnd -= 1; // 选区停在下一段的开头：不算下一段
    const slice = pid >= 0 ? wrBlockSlice(block, range) : null;
    blockRef.current = slice && slice.text.trim() ? { pid, pidEnd: pidEnd >= pid ? pidEnd : pid, ...slice } : null;
    const r = range.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left + r.width / 2 };
  };

  const intact = (range) => {
    const ed = editorRef.current;
    if (!ed || !range || range.collapsed) return false;
    if (!ed.contains(range.startContainer) || !ed.contains(range.endContainer)) return false;
    return range.toString() === rangeTextRef.current;
  };

  const returnFocus = (caret) => {
    const ed = editorRef.current;
    if (!ed || !ed.isConnected) return;
    const active = document.activeElement;
    const ours = !active || active === document.body
      || (popRef.current && popRef.current.contains(active))
      || (barRef.current && barRef.current.contains(active));
    if (!ours) return; // 作者已经去了别处（例如另一栏的输入框）：不抢
    ed.focus({ preventScroll: true });
    const sel = window.getSelection();
    if (!sel) return;
    if (caret && ed.contains(caret.startContainer)) {
      sel.removeAllRanges();
      sel.addRange(caret);
      return;
    }
    const range = rangeRef.current;
    if (range && intact(range)) {
      quietRef.current = range.cloneRange();
      sel.removeAllRanges();
      sel.addRange(range.cloneRange());
    }
  };

  const quietMatches = (sel) => {
    if (!quietRef.current || !sel || !sel.rangeCount) return false;
    if (wrSameRange(sel.getRangeAt(0), quietRef.current)) return true;
    quietRef.current = null;
    return false;
  };

  const forget = () => {
    rangeRef.current = null;
    textRef.current = "";
    rangeTextRef.current = "";
    blockRef.current = null;
  };

  return {
    rangeRef, textRef, rangeTextRef, blockRef,
    capture, intact, returnFocus, quietMatches, forget,
    wake: () => { quietRef.current = null; },
  };
}
