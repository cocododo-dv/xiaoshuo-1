import React from "react";
import { unwrapNode } from "./manuscript-html.js";
import { WR_RW_ACTIONS } from "./ws-writer-ai.js";
import { wrCaretAfter } from "./ws-writer-inline-parts.jsx";
import { wrDecidePatch, wrRequestRewrite } from "./ws-writer-requests.js";

/* ==========================================================
   选区工具条 · 改写（2026-09-30 从 ws-writer-inline.jsx 拆出，审计 F03-18）
   ----------------------------------------------------------
   useInlineRewrite({ sceneId, selection, findingRef, disabled, setPhase, onCommit })：
   · run(instr)：把选中的字送去改写（带着深改交来的发现），出几版候选；后发的请求作废先发的。
     每一次请求的候选裁决把手（patch）跟着这一次走：采纳 = accept，换一次请求 / 关掉 = reject。
   · replace()：用选中的那一版替换原选区，原处留一个「可还原」标记（界面标记，只标到离开这一场为止）；
     那段字在改写期间被改过时不替换（staleSel），候选留着给作者复制。返回 { done, caret }。
   · openRev(span) / revert() / accept()：点正文里的改写标记——还原成原来的字，或留下改写、去掉标记。
   · reset()：弹层收起时作废在飞的请求、弃用没采纳的候选、清掉结果；clearResults()：只清结果（切到只读时）。
   ESM 模块，不写 window。
   ========================================================== */

const { useRef, useState } = React;

export function useInlineRewrite({ sceneId, selection, findingRef, disabled, setPhase, onCommit }) {
  const [results, setResults] = useState([]);
  const [pick, setPick] = useState(0);
  const [error, setError] = useState(null);
  const [custom, setCustom] = useState("");
  const [tone, setTone] = useState({ warm: 50, expand: 50, direct: 50 });
  const [staleSel, setStaleSel] = useState(false);   // 选中的那段字在改写期间变了：不再原样替换
  const [copied, setCopied] = useState(false);
  const lastInstr = useRef(null);
  const patchRef = useRef(null);
  const reqRef = useRef(0);
  const revElRef = useRef(null);

  const run = async (instr) => {
    if (disabled) return;
    const id = ++reqRef.current;
    if (patchRef.current) { wrDecidePatch(patchRef.current, 0, false); patchRef.current = null; }
    lastInstr.current = instr;
    setPhase("loading"); setError(null); setStaleSel(false);
    try {
      const { texts, patch } = await wrRequestRewrite({ sceneId, text: selection.textRef.current, instruction: instr, finding: findingRef.current });
      if (reqRef.current !== id) { wrDecidePatch(patch, 0, false); return; }
      patchRef.current = patch;
      setResults(texts); setPick(0); setPhase("result");
    } catch (err) {
      if (reqRef.current !== id) return;
      setError(err); setPhase("error");
    }
  };

  const replace = () => {
    const range = selection.rangeRef.current;
    const chosen = results[pick];
    let caret = null;
    if (range && chosen && !disabled) {
      if (!selection.intact(range)) { setStaleSel(true); setCopied(false); return { done: false }; } // 候选留着，作者可以复制或重新选中再改
      try {
        const span = document.createElement("span");
        span.className = "wr-rev";
        span.setAttribute("data-orig", selection.textRef.current);
        span.textContent = chosen;
        range.deleteContents(); range.insertNode(span);
        caret = wrCaretAfter(span); // 光标落在改写过的那一句后面
      } catch (e) {}
      const sel = window.getSelection(); if (sel) sel.removeAllRanges();
      if (onCommit) onCommit();
      wrDecidePatch(patchRef.current, pick, true); // 采纳回传（服务端记下去留、再过一遍抄袭门）
      patchRef.current = null;
    }
    return { done: true, caret };
  };

  const copy = async () => {
    const chosen = results[pick];
    if (!chosen) return;
    try {
      await navigator.clipboard.writeText(chosen);
      setCopied(true);
    } catch (e) { setCopied(false); }
  };

  /* ---- AI 改写标记（本次打开时可还原） ---- */
  const openRev = (span) => {
    revElRef.current = span;
    const r = span.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left + r.width / 2 };
  };
  const revert = () => {
    const span = revElRef.current;
    let caret = null;
    if (span && span.parentNode) {
      const parent = span.parentNode;
      const text = document.createTextNode(span.getAttribute("data-orig") || "");
      parent.replaceChild(text, span);
      caret = wrCaretAfter(text);
      if (parent.normalize) parent.normalize();
    }
    if (onCommit) onCommit();
    return caret;
  };
  const accept = () => {
    const span = revElRef.current;
    const caret = wrCaretAfter(span);
    const parent = span && span.parentNode;
    if (parent) {
      unwrapNode(span);
      if (parent.normalize) parent.normalize();
    }
    return caret;
  };

  const reset = () => {
    reqRef.current += 1;
    if (patchRef.current) { wrDecidePatch(patchRef.current, 0, false); patchRef.current = null; } // 没采纳就回传弃用
    setResults([]); setPick(0); setError(null); setCustom(""); setStaleSel(false); setCopied(false);
    revElRef.current = null;
  };

  return {
    results, pick, error, custom, tone, staleSel, copied,
    choose: (i) => { setPick(i); setCopied(false); },
    setCustom,
    tuneTone: (patch) => setTone((t) => ({ ...t, ...patch })),
    run,
    retry: () => run(lastInstr.current || WR_RW_ACTIONS[0].instr),
    replace, copy, openRev, revert, accept,
    revSpan: () => revElRef.current,
    revCaret: () => wrCaretAfter(revElRef.current),
    clearResults: () => { setResults([]); setError(null); },
    reset,
  };
}
