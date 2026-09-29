import React from "react";
import { focusableIn, isImeComposing } from "./ws-dialog.jsx";
import { emit, useWindowEvents } from "./lib/events.js";
import { WR_RW_ACTIONS } from "./ws-writer-ai.js";
import { wrDecidePatch, wrRequestRewrite } from "./ws-writer-requests.js";
import {
  WR_ANNO_MAX_ITEMS, WR_ANNO_MAX_QUOTE,
  wrAnnoAnchor, wrAnnoId, wrAnnoLoad, wrAnnoMark, wrAnnoRetitle, wrAnnoSave, wrAnnoUnmark,
} from "./ws-writer-annotations.js";
import {
  WrAnnoPop, WrDeepSelectionBar, WrRevPop, WrRewriteBar, WrRewritePop, wrCaretAfter, wrSameRange,
} from "./ws-writer-inline-parts.jsx";
import { wrBlockSlice } from "./ws-writer-manuscript.js";
import { MANUSCRIPT_BLOCK_SELECTOR, unwrapNode } from "./manuscript-html.js";

/* ==========================================================
   选区工具条 + 改写 / 批注弹层（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   选中正文 → 工具条：润色 / 更凝练 / 更具象 / 对话化 / 批注 / 调音 / 自定义…
   改写走后端 passages/patch-candidates（writer_passage_patch 节点），替换后在原处留一个
   「可还原」标记——那是界面标记，只标到离开这一场为止（落盘的是干净正文）。
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
   画面部分（工具条、各个弹层）在 ws-writer-inline-parts.jsx，这里管状态、选区、请求与焦点。
   ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useRef, useState } = React;

function announceAnnotations(sceneId) {
  emit("ws:anno-change", { sid: sceneId });
}

/* finding / onFindingDone（2026-09-22 诊断统一）：深改面板「选中这一句去改写 / 按诊断改写」带过来的那条发现。
   带着它时每一次改写请求都附上发现的 id / 维度 / 改法（后端按维度定修补类别、偏好画像按维度学）；
   工具条多一个「按诊断改写」，autoRun 的发现一弹出工具条就直接出候选。
   作者把选区换到别处、关掉弹层、换场，这条发现就作废（onFindingDone）。 */
export function WrInlineRewrite({ editorRef, sceneId, annoKey, onCommit, readOnly = false, deep = false, onRewriteSelection, onOpenSettings, finding = null, onFindingDone, onPassageReview, passageBusy = false }) {
  const [rect, setRect] = useState(null);
  const [phase, setPhase] = useState("idle"); // idle | custom | tune | loading | result | error | anno | rev
  const [results, setResults] = useState([]);
  const [pick, setPick] = useState(0);
  const [error, setError] = useState(null);
  const [custom, setCustom] = useState("");
  const [popTop, setPopTop] = useState(null);
  const [annoText, setAnnoText] = useState("");
  const [annoNew, setAnnoNew] = useState(false);
  const [annoSaveError, setAnnoSaveError] = useState(null); // null | "storage"（写不进本机存储）| "full"（这一场满额）
  const [staleSel, setStaleSel] = useState(false);          // 选中的那段字在改写期间变了：不再原样替换
  const [copied, setCopied] = useState(false);
  const [tone, setTone] = useState({ warm: 50, expand: 50, direct: 50 });
  const lastInstr = useRef(null);
  const rangeRef = useRef(null);
  const selRef = useRef("");
  const selRangeTextRef = useRef(""); // 选区按 Range.toString() 算的字（替换前据此确认那段字还在原处、没被改过）
  const selBlockRef = useRef(null);   // 选区在起始段里的那一截 { pid, start, end, text }（深改 → 起草时按它重新选中）
  const popRef = useRef(null);
  const barRef = useRef(null);
  const quietRef = useRef(null);      // 关掉弹层后原样还回正文的选区：它不再弹工具条（作者刚按过 Esc）
  const focusBarRef = useRef(false);  // Alt+F10 要进工具条：等它画出来再聚焦
  const prevPhaseRef = useRef("idle");
  const annoRef = useRef(null);       // 正在看 / 写的批注：{ id, anchor?（新建时）, key（存储键）, sid（所属场景）}
  const revElRef = useRef(null);
  const patchRef = useRef(null);      // 这一次改写候选的裁决把手
  const reqRef = useRef(0);
  const findingRef = useRef(finding);       // 深改交来的发现（run 与选区处理读最新的）
  const findingDoneRef = useRef(onFindingDone);
  const autoRanRef = useRef(null);          // 「按诊断改写」只自动跑一次（按发现 id）
  useEffect(() => { findingRef.current = finding; findingDoneRef.current = onFindingDone; });
  const dropFinding = () => {
    autoRanRef.current = null;
    if (findingRef.current && findingDoneRef.current) findingDoneRef.current();
  };
  const locked = readOnly;

  /* 切到只读（批准锁定）时收起一切正在进行的弹层 */
  useEffect(() => {
    if (!locked) return;
    setPhase("idle"); setRect(null); setResults([]); setError(null);
  }, [locked]);
  /* 进出深改时，已经弹出的工具条作废（它是按另一种姿态画的） */
  useEffect(() => { setPhase("idle"); setRect(null); }, [deep]);

  /* 从当前选区取位置并弹出工具条；选区不在正文里、或太短时返回 false */
  const captureSelection = () => {
    const ed = editorRef.current;
    const sel = window.getSelection();
    if (!ed || !sel || sel.isCollapsed || sel.rangeCount === 0) return false;
    const range = sel.getRangeAt(0);
    if (!ed.contains(range.commonAncestorContainer)) return false;
    const text = sel.toString();
    if (text.trim().length < 2) return false;
    rangeRef.current = range.cloneRange();
    selRef.current = text;
    selRangeTextRef.current = range.toString();
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
    selBlockRef.current = slice && slice.text.trim() ? { pid, pidEnd: pidEnd >= pid ? pidEnd : pid, ...slice } : null;
    const r = range.getBoundingClientRect();
    setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
    return true;
  };

  useEffect(() => {
    if (locked) return undefined;
    const onSel = () => {
      if (phase !== "idle") return;
      // 焦点在工具条上（Alt+F10 进来的）：作者在操作工具条，不是在重新选字——别让选区的变动把它收掉
      if (barRef.current && barRef.current.contains(document.activeElement)) return;
      const sel = window.getSelection();
      if (quietRef.current && sel && sel.rangeCount) {
        if (wrSameRange(sel.getRangeAt(0), quietRef.current)) return;
        quietRef.current = null;
      }
      const captured = captureSelection();
      if (!captured) setRect(null);
      /* 选区换到了和发现无关的字：这条发现作废，接下来是普通改写 */
      const current = captured ? findingRef.current : null;
      if (current && current.evidence && current.evidence.excerpt) {
        const excerpt = String(current.evidence.excerpt);
        const picked = String(selRef.current || "");
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
    run(current.recommendation || WR_RW_ACTIONS[0].instr);
  }, [rect, phase, finding]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 焦点在工具条 / 弹层里（或随着它们消失掉到了 <body> 上）时，把焦点还给正文。
     caret：动过正文之后光标该落的位置；没有就把打开前的选区原样还回去（它不再弹工具条）。 */
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
    if (range && selectionIntact(range)) {
      quietRef.current = range.cloneRange();
      sel.removeAllRanges();
      sel.addRange(range.cloneRange());
    }
  };

  /* caret：见 returnFocus；refocus=false：换场等不是作者在弹层里做的动作，不动焦点 */
  const close = ({ caret = null, refocus = true } = {}) => {
    if (refocus) returnFocus(caret);
    reqRef.current += 1;
    if (patchRef.current) { wrDecidePatch(patchRef.current, 0, false); patchRef.current = null; } // 没采纳就回传弃用
    setPhase("idle"); setRect(null); setResults([]); setPick(0); setError(null); setCustom(""); setAnnoText("");
    setAnnoSaveError(null); setStaleSel(false); setCopied(false);
    annoRef.current = null; revElRef.current = null;
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
    if (annoNew && annoRef.current) wrAnnoUnmark(editorRef.current, annoRef.current.id);
    close({ refocus: false });
    rangeRef.current = null; selRef.current = ""; selRangeTextRef.current = ""; selBlockRef.current = null;
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
        quietRef.current = null;
        if (captureSelection()) focusBarRef.current = true;
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
  }, [rect, phase, results, error, custom, annoText, tone]);

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
        revElRef.current = rev;
        const r = rev.getBoundingClientRect();
        setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
        setPhase("rev");
        return;
      }
      const mark = e.target.closest("mark.wr-anno");
      const id = mark && ed.contains(mark) ? mark.getAttribute("data-anno-id") : null;
      if (!id) return;
      e.preventDefault();
      const item = wrAnnoLoad(annoKey).find((anno) => anno.id === id);
      annoRef.current = { id, key: annoKey, sid: sceneId };
      setAnnoText(item ? item.note : "");
      setAnnoNew(false);
      setAnnoSaveError(null);
      const r = mark.getBoundingClientRect();
      setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
      setPhase("anno");
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, [locked, deep, annoKey, sceneId, editorRef]);

  const run = async (instr) => {
    if (locked || deep) return;
    const id = ++reqRef.current;
    if (patchRef.current) { wrDecidePatch(patchRef.current, 0, false); patchRef.current = null; }
    lastInstr.current = instr;
    setPhase("loading"); setError(null); setStaleSel(false);
    try {
      const { texts, patch } = await wrRequestRewrite({ sceneId, text: selRef.current, instruction: instr, finding: findingRef.current });
      if (reqRef.current !== id) { wrDecidePatch(patch, 0, false); return; }
      patchRef.current = patch;
      setResults(texts); setPick(0); setPhase("result");
    } catch (err) {
      if (reqRef.current !== id) return;
      setError(err); setPhase("error");
    }
  };
  /* 选区还是改写前那段字吗：还在这台编辑器里、没被改过、没塌成空的
     （换场 / 服务端草稿回填等整段重载后，Range 会塌到编辑器开头，insertNode 会把字插到正文最前面） */
  const selectionIntact = (range) => {
    const ed = editorRef.current;
    if (!ed || !range || range.collapsed) return false;
    if (!ed.contains(range.startContainer) || !ed.contains(range.endContainer)) return false;
    return range.toString() === selRangeTextRef.current;
  };
  const doReplace = () => {
    const range = rangeRef.current;
    const chosen = results[pick];
    let caret = null;
    if (range && chosen && !locked && !deep) {
      if (!selectionIntact(range)) { setStaleSel(true); setCopied(false); return; } // 候选留着，作者可以复制或重新选中再改
      try {
        const span = document.createElement("span");
        span.className = "wr-rev";
        span.setAttribute("data-orig", selRef.current);
        span.textContent = chosen;
        range.deleteContents(); range.insertNode(span);
        caret = wrCaretAfter(span); // 光标落在改写过的那一句后面
      } catch (e) {}
      const sel = window.getSelection(); if (sel) sel.removeAllRanges();
      if (onCommit) onCommit();
      wrDecidePatch(patchRef.current, pick, true); // 采纳回传（学习偏好）
      patchRef.current = null;
    }
    close({ caret });
  };

  const copyChosen = async () => {
    const chosen = results[pick];
    if (!chosen) return;
    try {
      await navigator.clipboard.writeText(chosen);
      setCopied(true);
    } catch (e) { setCopied(false); }
  };

  /* ---- 批注 ---- */
  /* 这条批注最后一个标注后面的光标（标注随后被拆掉时，活动 Range 跟着落到那段字后面） */
  const caretAfterAnno = (id) => {
    const ed = editorRef.current;
    if (!ed || !id) return null;
    const marks = Array.from(ed.querySelectorAll("mark.wr-anno")).filter((mark) => mark.getAttribute("data-anno-id") === id);
    return wrCaretAfter(marks[marks.length - 1]);
  };
  const startAnno = () => {
    const ed = editorRef.current;
    const range = rangeRef.current;
    if (!ed || !range || locked || deep) return;
    const anchor = wrAnnoAnchor(ed, range);
    if (!anchor || anchor.quote.length > WR_ANNO_MAX_QUOTE) return;
    const id = wrAnnoId();
    const marks = wrAnnoMark(ed, { id, note: "" }, anchor.start, anchor.end);
    if (!marks.length) return;
    annoRef.current = { id, anchor, key: annoKey, sid: sceneId };
    setAnnoText(""); setAnnoNew(true); setAnnoSaveError(null);
    const r = marks[0].getBoundingClientRect();
    setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
    setPhase("anno");
    const sel = window.getSelection(); if (sel) sel.removeAllRanges();
  };
  /* 批注不是正文：存进本机浏览器的批注清单，不触发正文保存 */
  const saveAnno = () => {
    const current = annoRef.current;
    const note = annoText.trim();
    const ed = editorRef.current;
    if (!current || !ed) { close(); return; }
    if (!note) { deleteAnno(); return; }
    const key = current.key || annoKey;
    const list = wrAnnoLoad(key);
    const now = Date.now();
    const existing = list.find((anno) => anno.id === current.id);
    if (!existing && !current.anchor) { close(); return; } // 点开的旧批注已经在别处删掉了：没有引文可以重建
    if (!existing && list.length >= WR_ANNO_MAX_ITEMS) { setAnnoSaveError("full"); return; }
    const next = existing
      ? list.map((anno) => (anno.id === current.id ? { ...anno, note, updatedAt: now } : anno))
      : [...list, { id: current.id, quote: current.anchor.quote, prefix: current.anchor.prefix, suffix: current.anchor.suffix, note, createdAt: now, updatedAt: now }];
    if (!wrAnnoSave(key, next)) { setAnnoSaveError("storage"); return; }
    wrAnnoRetitle(ed, current.id, note);
    announceAnnotations(current.sid || sceneId);
    close({ caret: caretAfterAnno(current.id) });
  };
  const deleteAnno = () => {
    const current = annoRef.current;
    const caret = current ? caretAfterAnno(current.id) : null;
    if (current) {
      const key = current.key || annoKey;
      wrAnnoUnmark(editorRef.current, current.id);
      const list = wrAnnoLoad(key);
      if (list.some((anno) => anno.id === current.id)) wrAnnoSave(key, list.filter((anno) => anno.id !== current.id));
      announceAnnotations(current.sid || sceneId);
    }
    close({ caret });
  };
  const cancelAnno = () => {
    const caret = annoRef.current ? caretAfterAnno(annoRef.current.id) : null;
    if (annoNew && annoRef.current) wrAnnoUnmark(editorRef.current, annoRef.current.id);
    close({ caret });
  };

  /* ---- AI 改写标记（本次打开时可还原） ---- */
  const unwrapRev = (span) => {
    const parent = span && span.parentNode;
    if (!parent) return;
    unwrapNode(span);
    if (parent.normalize) parent.normalize();
  };
  const revertRev = () => {
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
    close({ caret });
  };
  const acceptRev = () => {
    const caret = wrCaretAfter(revElRef.current);
    unwrapRev(revElRef.current);
    close({ caret });
  };
  /* Esc / 「取消」「关闭」：收起最上面这一层（新批注按取消处理；改写标记的弹层把光标放回那一处后面） */
  const dismiss = () => {
    if (phase === "anno") cancelAnno();
    else if (phase === "rev") close({ caret: wrCaretAfter(revElRef.current) });
    else close();
  };

  if (!rect || locked) return null;
  const below = rect.top < 170;
  const pos = (w) => ({
    top: below ? rect.bottom + 8 : rect.top - 8,
    left: Math.min(Math.max(rect.left, w / 2 + 12), window.innerWidth - w / 2 - 12),
    transform: below ? "translate(-50%, 0)" : "translate(-50%, -100%)",
  });
  const popStyle = { top: popTop != null ? popTop : (rect.bottom + 10), left: Math.min(Math.max(rect.left, 192), window.innerWidth - 192), transform: "translateX(-50%)" };

  if (phase === "idle" && deep) {
    return (
      <WrDeepSelectionBar barRef={barRef} style={pos(onPassageReview ? 340 : 220)} slice={selBlockRef.current}
        onPassageReview={onPassageReview} passageBusy={passageBusy}
        onReviewPassage={() => {
          const slice = selBlockRef.current;
          setRect(null);
          if (slice) onPassageReview({ pid: slice.pid, pidEnd: slice.pidEnd, find: slice.text });
        }}
        onRewriteSelection={() => {
          const slice = selBlockRef.current;
          setRect(null);
          if (onRewriteSelection && slice) onRewriteSelection({ pid: slice.pid, find: slice.text, start: slice.start, end: slice.end });
        }} />
    );
  }
  if (phase === "idle") {
    return (
      <WrRewriteBar barRef={barRef} style={pos(448)} finding={finding} quoteLength={selRangeTextRef.current.length}
        onRun={run} onStartAnno={startAnno} onTune={() => setPhase("tune")} onCustom={() => setPhase("custom")} />
    );
  }
  if (phase === "rev") {
    const span = revElRef.current;
    return (
      <WrRevPop popRef={popRef} style={popStyle}
        orig={span ? (span.getAttribute("data-orig") || "") : ""} now={span ? span.textContent : ""}
        onDismiss={dismiss} onRevert={revertRev} onAccept={acceptRev} />
    );
  }
  if (phase === "anno") {
    return (
      <WrAnnoPop popRef={popRef} style={popStyle} annoNew={annoNew} annoText={annoText} onAnnoText={setAnnoText}
        annoSaveError={annoSaveError} onCancel={cancelAnno} onDelete={deleteAnno} onSave={saveAnno} />
    );
  }
  const retry = () => run(lastInstr.current || WR_RW_ACTIONS[0].instr);
  return (
    <WrRewritePop popRef={popRef} style={popStyle} phase={phase} finding={finding} selText={selRef.current}
      results={results} pick={pick} onPick={(i) => { setPick(i); setCopied(false); }}
      custom={custom} onCustom={setCustom} tone={tone} onTone={(patch) => setTone((t) => ({ ...t, ...patch }))}
      error={error} staleSel={staleSel} copied={copied} onCopy={copyChosen}
      onRun={run} onRetry={retry} onDismiss={dismiss} onReplace={doReplace}
      onOpenSettings={onOpenSettings ? () => { close({ refocus: false }); onOpenSettings(); } : null} />
  );
}
