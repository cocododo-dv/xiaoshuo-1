import React from "react";
import { WR_RW_ACTIONS, wrAiLocalError } from "./ws-writer-ai.js";
import { wrCaretAfter, wrRevAccept, wrRevGroup, wrRevRevert, wrRevSplice } from "./ws-writer-rev.js";
import { wrDecidePatch, wrRequestRewrite } from "./ws-writer-requests.js";

/* ==========================================================
   选区工具条 · 改写（2026-09-30 从 ws-writer-inline.jsx 拆出，审计 F03-18；2026-10 跨段按段换回，重评 R12）
   ----------------------------------------------------------
   useInlineRewrite({ editorRef, sceneId, selection, findingRef, disabled, setPhase, onCommit })：
   · run(instr)：把选中的字（按段，一段一行）送去改写（带着深改交来的发现），出几版候选；后发的请求作废先发的。
     每一次请求的候选裁决把手（patch）跟着这一次走：采纳 = accept，换一次请求 / 关掉 = reject。
     每一版候选是几段字（results[i].paragraphs）；选了几段的选区，候选挤成一段的那一版不给（换进去会把段落并掉）。
   · replace()：用选中的那一版按段换掉原选区（ws-writer-rev.js：候选第一段接在起始段选区之前那一截后面，最后一段接上
     结束段选区之后那一截，中间各成一段），原处留一个「可还原」标记（界面标记，只标到离开这一场为止）；
     那段字在改写期间被改过时不替换（staleSel），候选留着给作者复制。返回 { done, caret }。
   · openRev(span) / revert() / accept()：点正文里的改写标记——还原成原来的几段，或留下改写、去掉标记。
     改写之后那几段又改过时不还原（revertRefused），说清楚去版本历史里找。
   · reset()：弹层收起时作废在飞的请求、弃用没采纳的候选、清掉结果；clearResults()：只清结果（切到只读时）。
   ESM 模块，不写 window。
   ========================================================== */

const { useRef, useState } = React;

export function useInlineRewrite({ editorRef, sceneId, selection, findingRef, disabled, setPhase, onCommit }) {
  const [results, setResults] = useState([]);   // [{ paragraphs, text, collapsed }]
  const [pick, setPick] = useState(0);
  const [error, setError] = useState(null);
  const [custom, setCustom] = useState("");
  const [tone, setTone] = useState({ warm: 50, expand: 50, direct: 50 });
  const [staleSel, setStaleSel] = useState(false);   // 选中的那段字在改写期间变了：不再原样替换
  const [copied, setCopied] = useState(false);
  const [revertRefused, setRevertRefused] = useState(false);
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
      const segments = selection.segmentsRef.current;
      if (segments && segments.unsupported) throw wrAiLocalError("selection-unsupported");
      const { results: next, patch } = await wrRequestRewrite({
        sceneId, text: selection.textRef.current, instruction: instr, finding: findingRef.current,
        sourceParagraphs: segments ? segments.paragraphs.length : null,
      });
      if (reqRef.current !== id) { wrDecidePatch(patch, 0, false); return; }
      patchRef.current = patch;
      setResults(next); setPick(0); setPhase("result");
    } catch (err) {
      if (reqRef.current !== id) return;
      setError(err); setPhase("error");
    }
  };

  /* 送去改写的那几段字还在原处吗：Range 没塌、按段切出来的还是送出去的那几段 */
  const liveSegmentsIfIntact = () => {
    const range = selection.rangeRef.current;
    if (!range || !selection.intact(range)) return null;
    const live = selection.liveSegments();
    if (!live || live.unsupported || live.text !== selection.textRef.current) return null;
    return live;
  };

  const replace = () => {
    const chosen = results[pick];
    const ed = editorRef.current;
    let caret = null;
    if (selection.rangeRef.current && chosen && ed && !disabled) {
      const live = liveSegmentsIfIntact();
      if (!live) { setStaleSel(true); setCopied(false); return { done: false }; } // 候选留着，作者可以复制或重新选中再改
      try {
        caret = wrRevSplice(ed, live, chosen.paragraphs).caret; // 光标落在改写过的最后一段后面
      } catch (e) {
        setStaleSel(true); setCopied(false); return { done: false };
      }
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
      await navigator.clipboard.writeText(chosen.text);
      setCopied(true);
    } catch (e) { setCopied(false); }
  };

  /* ---- AI 改写标记（本次打开时可还原） ---- */
  const openRev = (span) => {
    revElRef.current = span;
    setRevertRefused(false);
    const r = span.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left + r.width / 2 };
  };
  /* 还原：返回 { done, caret }；那几段改写之后又改过（或这一处不是这一次打开里换进来的）不还原，弹层留着说原因 */
  const revert = () => {
    const result = wrRevRevert(revElRef.current);
    if (!result.ok) { setRevertRefused(true); return { done: false }; }
    if (onCommit) onCommit();
    return { done: true, caret: result.caret };
  };
  const accept = () => wrRevAccept(revElRef.current);

  const reset = () => {
    reqRef.current += 1;
    if (patchRef.current) { wrDecidePatch(patchRef.current, 0, false); patchRef.current = null; } // 没采纳就回传弃用
    setResults([]); setPick(0); setError(null); setCustom(""); setStaleSel(false); setCopied(false); setRevertRefused(false);
    revElRef.current = null;
  };

  return {
    results, pick, error, custom, tone, staleSel, copied, revertRefused,
    choose: (i) => { setPick(i); setCopied(false); },
    setCustom,
    tuneTone: (patch) => setTone((t) => ({ ...t, ...patch })),
    run,
    retry: () => run(lastInstr.current || WR_RW_ACTIONS[0].instr),
    replace, copy, openRev, revert, accept,
    revSpan: () => revElRef.current,
    revGroup: () => wrRevGroup(revElRef.current),
    revCaret: () => wrCaretAfter(revElRef.current),
    clearResults: () => { setResults([]); setError(null); },
    reset,
  };
}
