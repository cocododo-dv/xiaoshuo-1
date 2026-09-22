import React from "react";
import { I } from "./icons.jsx";
import { CloseButton, Spinner } from "./ws-ui.jsx";
import { wrPickedText, wrSentences } from "./writer-candidates.js";
import { useWrInert } from "./ws-writer-hooks.js";
import { isImeComposing, useFocusTrap } from "./ws-dialog.jsx";
import { WrAiErrorBlock, WrCandidateList, WrContinueChips, useWrContinuation } from "./ws-writer-candidates.jsx";

/* ==========================================================
   AI 续写托盘（2026-09-21 从 ws-writer.jsx 拆出）
   打开时只是准备好：提示框获得焦点，作者按回车或「生成三条」才发请求——
   过去一打开就连发三次生成，作者还没来得及写一句要求。ESM 模块，不写 window。
   托盘是模态的：遮罩盖住了正文，焦点也关在托盘里（useFocusTrap），关上时由写作台把焦点
   还给正文和原来的光标（所以这里不让陷阱自己还焦点）。
   候选的快捷键（1 / 2 / 3 切换、回车采纳、R 重新生成）只认托盘里、不是控件上的按键：
   焦点在按钮上时回车是按那个按钮，在提示框里是打字；输入法组字的回车不是命令。
   过去它挂在 window 上什么都收——焦点回到正文时作者敲的数字被吞掉、R 又发一轮生成、
   选拼音的那一下回车把候选插进了正文。
   ========================================================== */

const { memo, useEffect, useRef, useState } = React;

/* 这些元素自己处理回车 / 字符键：按在它们上面的键不是候选快捷键 */
const WR_TRAY_OWN_KEYS = "button, a[href], input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='button'], [role='radio'], [role='tab']";

function wrTrayShortcutTarget(tray, target) {
  if (!tray || !target || !tray.contains(target)) return false;
  return !(target.closest && target.closest(WR_TRAY_OWN_KEYS));
}

function WrTrayImpl({ open, onClose, onAdopt, onMerge, onAdoptText, sceneLabel, pov, sceneId, design, onOpenSettings }) {
  const c = useWrContinuation(sceneId);
  const [sel, setSel] = useState(0);
  const [seed, setSeed] = useState(0);
  const inputRef = useRef(null);
  const regenerate = () => { setSeed((v) => v + 1); setSel(0); c.run(); };
  const trayRef = useRef(null);
  /* 焦点陷阱在打开的那一刻就把焦点放进提示框；可托盘刚打开时还带着 inert（收起时标的，副作用里才摘），
     浏览器会拒绝那一下聚焦——下一帧再放一次。焦点已经在托盘里就不动（作者可能已经 Tab 到别处）。 */
  useEffect(() => {
    if (!open) return undefined;
    const id = requestAnimationFrame(() => {
      const tray = trayRef.current;
      if (tray && tray.contains(document.activeElement)) return;
      if (inputRef.current) inputRef.current.focus();
    });
    return () => cancelAnimationFrame(id);
  }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (c.phase !== "result" || e.defaultPrevented || isImeComposing(e)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!wrTrayShortcutTarget(trayRef.current, e.target)) return;
      if (["1", "2", "3"].includes(e.key)) { e.preventDefault(); setSel(+e.key - 1); }
      else if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const cand = c.cands[sel];
        if (!cand) return;
        const picked = c.picks[cand.id] || [];
        if (picked.length && onAdoptText) onAdoptText(wrPickedText(wrSentences(cand.html), picked));
        else onAdopt(cand);
      }
      else if (e.key.toLowerCase() === "r") { e.preventDefault(); regenerate(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, c.phase, sel, c.picks, c.cands]); // eslint-disable-line react-hooks/exhaustive-deps
  const loading = c.phase === "loading";
  useWrInert(trayRef, !open);
  useFocusTrap(trayRef, open, { initialFocus: inputRef, restoreFocus: false });
  const hasResult = c.phase === "result" && c.cands.length > 0;
  return (
    <div ref={trayRef} className={`wr-tray ${open ? "show" : ""}`} role="dialog" aria-modal={open ? "true" : "false"} aria-label="AI 续写" tabIndex={-1}>
      <div className="wr-tray-grip" />
      <header className="wr-tray-head">
        <span className="wr-tray-spark" aria-hidden="true"><I.Sparkles size={16} /></span>
        <div className="wr-tray-heading">
          <div className="wr-tray-title">AI 续写</div>
          <div className="wr-tray-sub">{sceneLabel || "—"}{pov ? <span className="wr-tray-pov">视角：{pov}</span> : null}</div>
        </div>
        <CloseButton className="wr-tray-x" label="关闭续写（Esc）" onClick={onClose} />
      </header>
      <div className="wr-tray-prompt">
        <div className="wr-tray-prompt-main">
          <textarea ref={inputRef} className="wr-prompt-in" rows={2} value={c.prompt} aria-label="续写提示"
            onChange={(e) => c.setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); if (!loading) regenerate(); }
            }} />
          <WrContinueChips design={design} onPick={(text) => { c.setPrompt(text); if (inputRef.current) inputRef.current.focus(); }} />
        </div>
        <button type="button" className="btn btn-accent btn-sm" disabled={loading} onClick={regenerate}>
          {loading ? <Spinner size={13} /> : <I.Wand size={13} />} {loading ? "正在生成…" : c.phase === "result" || c.phase === "error" ? "重新生成" : "生成三条"}
        </button>
      </div>
      {/* 有候选时这一栏可以用 Tab 走到：焦点停在这里（或点过候选卡）时 1 / 2 / 3 / 回车 / R 生效 */}
      <div className="wr-cands" key={seed}
        {...(hasResult ? { tabIndex: 0, role: "group", "aria-label": "三条续写：按 1、2、3 切换，回车采纳，R 重新生成", "aria-keyshortcuts": "1 2 3 Enter R" } : {})}>
        {c.phase === "ready" && (
          <p className="wr-tray-hint">写一句要求（或点上面的快捷词），按回车生成。续写只在正文末尾追加下一段，不改已写的文字。</p>
        )}
        {c.phase === "error" && <WrAiErrorBlock error={c.error} onRetry={regenerate} onOpenSettings={onOpenSettings} />}
        <WrCandidateList state={c} selected={sel} onSelect={setSel} onAdopt={onAdopt} onAdoptText={onAdoptText} onMerge={onMerge} />
      </div>
      <footer className="wr-tray-foot">
        <span className="wr-tray-keys">
          <span><kbd className="wr-kbd">1</kbd><kbd className="wr-kbd">2</kbd><kbd className="wr-kbd">3</kbd>切换</span>
          <span><kbd className="wr-kbd">⏎</kbd>采纳</span>
          <span><kbd className="wr-kbd">R</kbd>重新生成</span>
          <span><kbd className="wr-kbd">Esc</kbd>关闭</span>
        </span>
        <span>点句子可以只挑几句</span>
      </footer>
    </div>
  );
}

export const WrTray = memo(WrTrayImpl);
