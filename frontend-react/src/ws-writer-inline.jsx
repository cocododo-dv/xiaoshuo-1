import React from "react";
import { focusableIn, isImeComposing } from "./ws-dialog.jsx";
import { useWindowEvents } from "./lib/events.js";
import { WR_RW_ACTIONS } from "./ws-writer-ai.js";
import { WrAnnoPop, WrDeepSelectionBar, WrRevPop, WrRewriteBar, WrRewritePop, wrRewritePopWide } from "./ws-writer-inline-parts.jsx";
import { useEditorSelection } from "./ws-writer-inline-selection.js";
import { useInlineRewrite } from "./ws-writer-inline-rewrite.js";
import { useInlineAnnotation } from "./ws-writer-inline-anno.js";

/* ==========================================================
   选区工具条 + 改写 / 批注弹层（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   选中正文 → 工具条：润色 / 更凝练 / 更具象 / 对话化 / 批注 / 调音 / 自定义…
   改写走后端 passages/patch-candidates（writer_passage_patch 节点）。选了几段就按段送、按段换回（重评 R12：
   候选第一段接在起始段选区之前那一截后面，最后一段接上结束段之后那一截，中间各成一段），替换后在原处留一个
   「可还原」标记——那是界面标记，只标到离开这一场为止（落盘的是干净正文）；还原时原来那几段原样回来。
   每次请求的候选裁决把手（patch）跟着这一次弹层走，采纳 = accept、关掉 = reject。
   批注存在本机浏览器（ws-writer-annotations.js），不进正文。
   readOnly（已批准终稿）：不出工具条，也不响应批注 / 改写标记的点击——那些动作都会改正文。
   换场（sceneId / annoKey 变了）：弹层、候选、没存的新批注一律作废——它们指的是上一场的正文。
   批注开始时就记下它属于哪一场、存到哪个键，保存 / 删除都写回那里，不跟着当前场走。
   deep（深改姿态，正文只读）：只给一个「回起草改这句」，把同一处选区带回起草姿态
   （跨段的选区带回起始段里的那一截，按段内偏移重新选中）。
   键盘：工具条在 DOM 末尾，Tab 走不过去——选中文字后按 Alt+F10 进工具条，←→ 在按钮间移动；
   弹层打开时焦点进弹层（结果出来落在选中的那一版上）；Esc / 取消 / 做完之后焦点回到正文：
   没动过正文就把原来的选区还回去（还回去的选区不再弹工具条），替换 / 批注之后光标放在那一处后面。
   过去焦点掉在 <body> 上，作者接着敲的字哪儿也去不了。输入法组字中的 Esc 只是撤掉拼音，不关弹层。
   分工：画面（工具条、各个弹层）在 ws-writer-inline-parts.jsx；选区在 ws-writer-inline-selection.js，
   改写与改写标记在 ws-writer-inline-rewrite.js，批注在 ws-writer-inline-anno.js；
   这里只管弹层走到哪一步（phase）、画在哪（rect）、键盘与焦点、深改交来的发现，以及收起时让三块各自清空。
   ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useRef, useState } = React;

/* finding / onFindingDone（2026-09-22 诊断统一）：深改面板「选中这一句去改写 / 按诊断改写」带过来的那条发现。
   带着它时每一次改写请求都附上发现的 id / 维度 / 改法（后端按维度定修补类别与改写策略）；
   工具条多一个「按诊断改写」，autoRun 的发现一弹出工具条就直接出候选。
   作者把选区换到别处、关掉弹层、换场，这条发现就作废（onFindingDone）。 */
export function WrInlineRewrite({ editorRef, sceneId, annoKey, onCommit, readOnly = false, deep = false, onRewriteSelection, onOpenSettings, finding = null, onFindingDone, onPassageReview, passageBusy = false }) {
  const [rect, setRect] = useState(null);
  const [phase, setPhase] = useState("idle"); // idle | custom | tune | loading | result | error | anno | rev
  const [popTop, setPopTop] = useState(null);
  const popRef = useRef(null);
  const barRef = useRef(null);
  const focusBarRef = useRef(false);  // Alt+F10 要进工具条：等它画出来再聚焦
  const prevPhaseRef = useRef("idle");
  const findingRef = useRef(finding);       // 深改交来的发现（改写请求与选区处理读最新的）
  const findingDoneRef = useRef(onFindingDone);
  const autoRanRef = useRef(null);          // 「按诊断改写」只自动跑一次（按发现 id）
  useEffect(() => { findingRef.current = finding; findingDoneRef.current = onFindingDone; });
  const dropFinding = () => {
    autoRanRef.current = null;
    if (findingRef.current && findingDoneRef.current) findingDoneRef.current();
  };
  const locked = readOnly;
  const selection = useEditorSelection({ editorRef, popRef, barRef });
  const rewrite = useInlineRewrite({ editorRef, sceneId, selection, findingRef, disabled: locked || deep, setPhase, onCommit });
  const anno = useInlineAnnotation({ editorRef, sceneId, annoKey, selection, disabled: locked || deep });

  /* 切到只读（批准锁定）时收起一切正在进行的弹层 */
  useEffect(() => {
    if (!locked) return;
    setPhase("idle"); setRect(null); rewrite.clearResults();
  }, [locked]); // eslint-disable-line react-hooks/exhaustive-deps
  /* 进出深改时，已经弹出的工具条作废（它是按另一种姿态画的） */
  useEffect(() => { setPhase("idle"); setRect(null); }, [deep]);

  useEffect(() => {
    if (locked) return undefined;
    const onSel = () => {
      if (phase !== "idle") return;
      // 焦点在工具条上（Alt+F10 进来的）：作者在操作工具条，不是在重新选字——别让选区的变动把它收掉
      if (barRef.current && barRef.current.contains(document.activeElement)) return;
      if (selection.quietMatches(window.getSelection())) return;
      const captured = selection.capture();
      setRect(captured);
      /* 选区换到了和发现无关的字：这条发现作废，接下来是普通改写 */
      const current = captured ? findingRef.current : null;
      if (current && current.evidence && current.evidence.excerpt) {
        const excerpt = String(current.evidence.excerpt);
        const picked = String(selection.textRef.current || "");
        if (!(picked.includes(excerpt) || excerpt.includes(picked))) dropFinding();
      }
    };
    document.addEventListener("selectionchange", onSel);
    return () => document.removeEventListener("selectionchange", onSel);
  }, [phase, locked]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 「按诊断改写」：深改把那一句选中、带着发现回到起草，工具条一弹出来就按发现的改法出候选 */
  useEffect(() => {
    if (phase !== "idle" || !rect || locked || deep) return;
    const current = finding;
    if (!current || !current.autoRun || autoRanRef.current === current.signal_id) return;
    autoRanRef.current = current.signal_id;
    rewrite.run(current.recommendation || WR_RW_ACTIONS[0].instr);
  }, [rect, phase, finding]); // eslint-disable-line react-hooks/exhaustive-deps

  /* caret：动过正文之后光标该落的位置（没有就把打开前的选区原样还回去）；
     refocus=false：换场等不是作者在弹层里做的动作，不动焦点 */
  const close = ({ caret = null, refocus = true } = {}) => {
    if (refocus) selection.returnFocus(caret);
    rewrite.reset();
    anno.reset();
    setPhase("idle"); setRect(null);
    dropFinding(); // 这一轮改写结束：深改交来的发现用过了
  };

  /* 换了一场（或换了作品）：上一场的选区、改写候选、没存的新批注全部作废。
     过去弹层留着——「替换为第 N 版」把上一场的改写插到这一场的开头并自动保存，
     「添加批注」把上一场的引文记进这一场的批注清单 */
  const scopeRef = useRef(`${sceneId || ""}|${annoKey || ""}`);
  useEffect(() => {
    const scope = `${sceneId || ""}|${annoKey || ""}`;
    if (scopeRef.current === scope) return;
    scopeRef.current = scope;
    anno.dropUnsaved();
    close({ refocus: false });
    selection.forget();
  }, [sceneId, annoKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const focusBar = () => {
    const first = barRef.current && focusableIn(barRef.current)[0];
    if (first) first.focus({ preventScroll: true });
  };

  /* 自己处理掉的 Esc 标记为已处理，写作台就不会再顺手收起侧栏。
     useWindowEvents 只挂一次监听、每次调用读最新的处理函数（过去每次渲染都拆了重挂）。 */
  useWindowEvents({
    keydown: (e) => {
      if (isImeComposing(e)) return; // 组字中的 Esc 只是撤掉拼音：过去它关掉弹层、丢掉写了一半的批注
      /* Alt+F10：从正文进工具条（选区还在、工具条被 Esc 收起过时重新弹出来） */
      if (e.key === "F10" && e.altKey) {
        const ed = editorRef.current;
        if (locked || !ed || !(e.target === ed || ed.contains(e.target)) || phase !== "idle") return;
        e.preventDefault();
        if (rect) { focusBar(); return; }
        selection.wake();
        const captured = selection.capture();
        if (captured) { setRect(captured); focusBarRef.current = true; }
        return;
      }
      if (e.key !== "Escape" || !(rect || phase !== "idle")) return;
      e.preventDefault();
      dismiss();
    },
  });

  /* Alt+F10 弹出的工具条画出来以后再把焦点放进去 */
  useEffect(() => {
    if (!focusBarRef.current || !barRef.current) return;
    focusBarRef.current = false;
    focusBar();
  });

  /* 弹层打开 / 换阶段时焦点进弹层：结果出来落在选中的那一版上，出错落在第一个动作上，
     加载中落在弹层本身。已经在弹层里（自动聚焦的输入框）就不动；从加载中等到结果时，
     作者若已经回到别处（不是弹层、也不是 <body>）就不抢。 */
  useEffect(() => {
    const prev = prevPhaseRef.current;
    prevPhaseRef.current = phase;
    if (phase === "idle" || phase === prev) return;
    const pop = popRef.current;
    if (!pop) return;
    const active = document.activeElement;
    if (active && active !== pop && pop.contains(active)) return;
    if (prev !== "idle" && active && active !== pop && active !== document.body) return;
    const target = (phase === "result" && pop.querySelector('[role="radio"][aria-checked="true"]'))
      || (phase !== "loading" && focusableIn(pop)[0]) || pop;
    target.focus({ preventScroll: true });
  }, [phase]);

  // measure the popover and clamp it inside the viewport (prevents header clipping)
  useEffect(() => {
    if (!rect || phase === "idle") { setPopTop(null); return; }
    const el = popRef.current;
    if (!el) return;
    const h = el.offsetHeight;
    const prefBelow = rect.bottom < window.innerHeight * 0.5;
    let top = prefBelow ? rect.bottom + 10 : rect.top - 10 - h;
    top = Math.min(Math.max(12, top), window.innerHeight - h - 12);
    setPopTop(top);
  }, [rect, phase, rewrite.results, rewrite.error, rewrite.custom, anno.text, rewrite.tone, rewrite.revertRefused]);

  /* 点正文里已有的批注 / 改写标记：打开对应的弹层 */
  useEffect(() => {
    if (locked || deep) return undefined;
    const onClick = (e) => {
      const ed = editorRef.current;
      if (!ed || !e.target.closest) return;
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) return;
      const rev = e.target.closest(".wr-rev");
      if (rev && ed.contains(rev)) {
        e.preventDefault();
        setRect(rewrite.openRev(rev));
        setPhase("rev");
        return;
      }
      const mark = e.target.closest("mark.wr-anno");
      const id = mark && ed.contains(mark) ? mark.getAttribute("data-anno-id") : null;
      if (!id) return;
      e.preventDefault();
      setRect(anno.openAt(mark, id));
      setPhase("anno");
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, [locked, deep, annoKey, sceneId, editorRef]); // eslint-disable-line react-hooks/exhaustive-deps

  const doReplace = () => {
    const result = rewrite.replace();
    if (result.done) close({ caret: result.caret });
  };
  /* 还原原文：改写之后那几段又改过时不还原，弹层留着说原因 */
  const doRevert = () => {
    const result = rewrite.revert();
    if (result.done) close({ caret: result.caret });
  };
  const startAnno = () => {
    const at = anno.start();
    if (!at) return;
    setRect(at);
    setPhase("anno");
  };
  const saveAnno = () => {
    const result = anno.save();
    if (result.close) close({ caret: result.caret });
  };
  const deleteAnno = () => close({ caret: anno.remove() });
  const cancelAnno = () => close({ caret: anno.cancel() });
  /* Esc / 「取消」「关闭」：收起最上面这一层（新批注按取消处理；改写标记的弹层把光标放回那一处后面） */
  const dismiss = () => {
    if (phase === "anno") cancelAnno();
    else if (phase === "rev") close({ caret: rewrite.revCaret() });
    else close();
  };

  if (!rect || locked) return null;
  const below = rect.top < 170;
  const pos = (w) => ({
    top: below ? rect.bottom + 8 : rect.top - 8,
    left: Math.min(Math.max(rect.left, w / 2 + 12), window.innerWidth - w / 2 - 12),
    transform: below ? "translate(-50%, 0)" : "translate(-50%, -100%)",
  });
  /* 选了几段、选得长、或候选 / 改写标记是几段时弹层放宽（wr-irw-pop.is-wide），定位按实际宽度夹在视口里 */
  const revGroup = phase === "rev" ? rewrite.revGroup() : null;
  const wide = phase === "anno" ? false : phase === "rev"
    ? !!revGroup && wrRewritePopWide([revGroup.original.length, revGroup.now.length], revGroup.now.join("").length)
    : wrRewritePopWide(
      [(selection.segmentsRef.current && selection.segmentsRef.current.paragraphs ? selection.segmentsRef.current.paragraphs.length : 1),
        ...rewrite.results.map((result) => result.paragraphs.length)],
      Array.from(selection.textRef.current || "").length,
    );
  const half = Math.min(wide ? 560 : 360, window.innerWidth * 0.88) / 2 + 12;
  const popStyle = { top: popTop != null ? popTop : (rect.bottom + 10), left: Math.min(Math.max(rect.left, half), window.innerWidth - half), transform: "translateX(-50%)" };

  if (phase === "idle" && deep) {
    return (
      <WrDeepSelectionBar barRef={barRef} style={pos(onPassageReview ? 340 : 220)} slice={selection.blockRef.current}
        onPassageReview={onPassageReview} passageBusy={passageBusy}
        onReviewPassage={() => {
          const slice = selection.blockRef.current;
          setRect(null);
          if (slice) onPassageReview({ pid: slice.pid, pidEnd: slice.pidEnd, find: slice.text });
        }}
        onRewriteSelection={() => {
          const slice = selection.blockRef.current;
          setRect(null);
          if (onRewriteSelection && slice) onRewriteSelection({ pid: slice.pid, find: slice.text, start: slice.start, end: slice.end });
        }} />
    );
  }
  if (phase === "idle") {
    return (
      <WrRewriteBar barRef={barRef} style={pos(448)} finding={finding} quoteLength={selection.rangeTextRef.current.length}
        onRun={rewrite.run} onStartAnno={startAnno} onTune={() => setPhase("tune")} onCustom={() => setPhase("custom")} />
    );
  }
  if (phase === "rev") {
    return (
      <WrRevPop popRef={popRef} style={popStyle} wide={wide} group={revGroup} refused={rewrite.revertRefused}
        onDismiss={dismiss} onRevert={doRevert} onAccept={() => close({ caret: rewrite.accept() })} />
    );
  }
  if (phase === "anno") {
    return (
      <WrAnnoPop popRef={popRef} style={popStyle} annoNew={anno.isNew} annoText={anno.text} onAnnoText={anno.setText}
        annoSaveError={anno.saveError} onCancel={cancelAnno} onDelete={deleteAnno} onSave={saveAnno} />
    );
  }
  return (
    <WrRewritePop popRef={popRef} style={popStyle} wide={wide} phase={phase} finding={finding} selText={selection.textRef.current}
      results={rewrite.results} pick={rewrite.pick} onPick={rewrite.choose}
      custom={rewrite.custom} onCustom={rewrite.setCustom} tone={rewrite.tone} onTone={rewrite.tuneTone}
      error={rewrite.error} staleSel={rewrite.staleSel} copied={rewrite.copied} onCopy={rewrite.copy}
      onRun={rewrite.run} onRetry={rewrite.retry} onDismiss={dismiss} onReplace={doReplace}
      onOpenSettings={onOpenSettings ? () => { close({ refocus: false }); onOpenSettings(); } : null} />
  );
}
