import React from "react";
import { I } from "./icons.jsx";
import { Notice, Spinner } from "./ws-ui.jsx";
import { focusableIn } from "./ws-dialog.jsx";
import { rovingIndex } from "./lib/keyboard.js";
import { WR_RW_ACTIONS, wrToneInstr } from "./ws-writer-ai.js";
import { WR_ANNO_MAX_ITEMS, WR_ANNO_MAX_NOTE, WR_ANNO_MAX_QUOTE } from "./ws-writer-annotations.js";
import { WrAiErrorBlock } from "./ws-writer-candidates.jsx";

/* ==========================================================
   选区工具条与弹层的画面部分（2026-09-29 从 ws-writer-inline.jsx 拆出）
   ----------------------------------------------------------
   WrInlineRewrite 管状态、选区、请求与焦点；这里只按它给的值画：深改姿态的工具条、起草姿态的工具条、
   改写标记弹层、批注弹层、改写弹层（自定义 / 调音 / 加载 / 出错 / 结果）。
   barRef / popRef 由 WrInlineRewrite 持有（焦点与定位要用），以 prop 传进来。
   另有两件小工具：选区是否同一处（wrSameRange）、工具条的方向键（紧跟节点后的光标 wrCaretAfter 住在 ws-writer-rev.js）。
   ESM 模块，不写 window。
   ========================================================== */

function WrToneSlider({ label, lo, hi, value, onChange }) {
  return (
    <div className="wr-tune-row">
      <div className="wr-tune-poles"><span>{lo}</span><span className="wr-tune-label">{label}</span><span>{hi}</span></div>
      <input type="range" min="0" max="100" value={value} aria-label={`${label}：${lo} 到 ${hi}`} className="wr-tune-range" onChange={(e) => onChange(+e.target.value)} />
    </div>
  );
}

export function wrSameRange(a, b) {
  try {
    return a.compareBoundaryPoints(Range.START_TO_START, b) === 0 && a.compareBoundaryPoints(Range.END_TO_END, b) === 0;
  } catch (e) { return false; }
}

/* 工具条里的 ←→ / Home / End：在按钮之间移动（role=toolbar 的键盘约定；下标规则同 lib/keyboard 的 rovingIndex，
   焦点不在工具条按钮上时 → 落到第一个、← 落到最后一个） */
const TOOLBAR_KEYS = ["ArrowLeft", "ArrowRight", "Home", "End"];
export function onToolbarKeyDown(event) {
  if (!TOOLBAR_KEYS.includes(event.key)) return;
  const buttons = focusableIn(event.currentTarget);
  if (!buttons.length) return;
  event.preventDefault();
  const at = buttons.indexOf(document.activeElement);
  const next = rovingIndex(event.key, at < 0 && event.key === "ArrowLeft" ? 0 : at, buttons.length);
  buttons[next].focus();
}

/* 深改姿态：只给「AI 看这一段 / 这几段」与「回起草改这句」 */
export function WrDeepSelectionBar({ barRef, style, slice, onPassageReview, passageBusy, onReviewPassage, onRewriteSelection }) {
  return (
    <div className="wr-irw-bar" ref={barRef} role="toolbar" aria-label="深改姿态下的选区" aria-keyshortcuts="Alt+F10" style={style}
      onMouseDown={(e) => e.preventDefault()} onKeyDown={onToolbarKeyDown}>
      <span className="wr-irw-spark" aria-hidden="true"><I.Pen size={13} /></span>
      {onPassageReview && (
        <button type="button" className="wr-irw-btn" disabled={passageBusy}
          title="让模型对着整场看选中的这几段：有没有要改的、和别处矛不矛盾、怎么改"
          onClick={onReviewPassage}>
          {slice && slice.pidEnd > slice.pid ? "AI 看这几段" : "AI 看这一段"}
        </button>
      )}
      <button type="button" className="wr-irw-btn accent" onClick={onRewriteSelection}>
        回起草改这句
      </button>
    </div>
  );
}

/* 起草姿态的工具条：按诊断改写（带着深改交来的发现时）/ 四个快捷改写 / 批注 / 调音 / 自定义… */
export function WrRewriteBar({ barRef, style, finding, quoteLength, onRun, onStartAnno, onTune, onCustom }) {
  return (
    <div className="wr-irw-bar" ref={barRef} role="toolbar" aria-label="改写选中的文字" aria-keyshortcuts="Alt+F10" style={style}
      onMouseDown={(e) => e.preventDefault()} onKeyDown={onToolbarKeyDown}>
      <span className="wr-irw-spark" aria-hidden="true"><I.Sparkles size={13} /></span>
      {finding && (
        <button type="button" className="wr-irw-btn accent" title={finding.issue || ""}
          onClick={() => onRun(finding.recommendation || WR_RW_ACTIONS[0].instr)}>
          按诊断改写
        </button>
      )}
      {WR_RW_ACTIONS.map((action) => (
        <button type="button" key={action.id} className="wr-irw-btn" onClick={() => onRun(action.instr)}>{action.label}</button>
      ))}
      <span className="wr-irw-sep" />
      {quoteLength > WR_ANNO_MAX_QUOTE
        ? <button type="button" className="wr-irw-btn" disabled title={`批注最多圈 ${WR_ANNO_MAX_QUOTE} 字，选短一点再批注`}>批注</button>
        : <button type="button" className="wr-irw-btn" onClick={onStartAnno}>批注</button>}
      <button type="button" className="wr-irw-btn" onClick={onTune}>调音</button>
      <button type="button" className="wr-irw-btn accent" onClick={onCustom}>自定义…</button>
    </div>
  );
}

/* 几段字按段画（段与段之间留出段距）：改写候选、原文、改写标记的对照都用它 */
export function WrParagraphs({ paragraphs }) {
  const list = Array.isArray(paragraphs) ? paragraphs : [paragraphs];
  return (
    <span className="wr-irw-paras">
      {list.map((text, i) => <span key={i} className="wr-irw-para">{text}</span>)}
    </span>
  );
}

/* 改写弹层要不要放宽：选了几段、选得长、或候选是几段时，360px 的窄弹层读起来太挤 */
export function wrRewritePopWide(paragraphCounts, textLength) {
  return paragraphCounts.some((count) => count > 1) || textLength > 240;
}

/* 点开正文里的改写标记：原文 / 改写对照（按段），还原或保留。
   group 为空：这一处不是这一次打开里换进来的（记录找不到），只能保留；group.intact 为假或 refused：改写之后那几段
   又改过，不能整段还原——打开时就说，「还原原文」不可点。 */
export function WrRevPop({ popRef, style, wide = false, group, refused = false, onDismiss, onRevert, onAccept }) {
  const blocked = refused || !group || !group.intact;
  return (
    <div className={`wr-irw-pop ${wide ? "is-wide" : ""}`} ref={popRef} tabIndex={-1} role="dialog" aria-label="AI 改写的这一处" style={style} onMouseDown={(e) => e.stopPropagation()}>
      <div className="wr-irw-head"><I.Sparkles size={14} /> 这一处是 AI 改写的 <span className="sp">离开这一场后不再标出</span></div>
      <div className="wr-irw-body">
        {blocked && (
          <Notice tone="warn" className="wr-irw-stale" role="alert">
            改写后又改过这几段，不能整段还原；可在版本历史里找回。
          </Notice>
        )}
        {group && (
          <>
            <div className="wr-rev-row"><span className="wr-rev-tag">原文</span><div className="wr-irw-orig wr-rev-text"><WrParagraphs paragraphs={group.original} /></div></div>
            <div className="wr-rev-row"><span className="wr-rev-tag now">改写</span><div className="wr-irw-new wr-rev-text"><WrParagraphs paragraphs={group.now} /></div></div>
          </>
        )}
      </div>
      <div className="wr-irw-foot">
        <button type="button" className="btn btn-quiet btn-sm" onClick={onDismiss}>关闭</button>
        <button type="button" className="btn btn-ghost btn-sm" disabled={blocked} onClick={onRevert}>还原原文</button>
        <button type="button" className="btn btn-accent btn-sm" onClick={onAccept}>保留改写</button>
      </div>
    </div>
  );
}

/* 批注弹层：新建 / 查看已有；存在本机浏览器 */
export function WrAnnoPop({ popRef, style, annoNew, annoText, onAnnoText, annoSaveError, onCancel, onDelete, onSave }) {
  return (
    <div className="wr-irw-pop" ref={popRef} tabIndex={-1} role="dialog" aria-label="批注" style={style} onMouseDown={(e) => e.stopPropagation()}>
      <div className="wr-irw-head"><I.FileText size={14} /> 批注 <span className="sp">{annoNew ? "新建" : "已有"}</span></div>
      <div className="wr-anno-note">
        <textarea autoFocus value={annoText} maxLength={WR_ANNO_MAX_NOTE} aria-label="批注内容" placeholder="写下对这段文字的批注、疑问或待办…" onChange={(e) => onAnnoText(e.target.value)} />
        {annoText.length >= WR_ANNO_MAX_NOTE * 0.8 && (
          <p className="wr-anno-scope wr-anno-count" aria-live="polite">
            {annoText.length >= WR_ANNO_MAX_NOTE ? `已到 ${WR_ANNO_MAX_NOTE} 字上限` : `${annoText.length} / ${WR_ANNO_MAX_NOTE} 字`}
          </p>
        )}
        <p className="wr-anno-scope">批注保存在本机浏览器里，不进正文，也不会同步到服务器或其他设备。</p>
        {annoSaveError === "storage" && <p className="wr-anno-scope is-error" role="alert">这台浏览器不让写入本地存储（隐私模式或存储已满），批注没有存下来。</p>}
        {annoSaveError === "full" && <p className="wr-anno-scope is-error" role="alert">这一场已经有 {WR_ANNO_MAX_ITEMS} 条批注，到上限了，这一条没有存下来。先在批注页签删掉几条用不着的再加。</p>}
      </div>
      <div className="wr-irw-foot">
        <button type="button" className="btn btn-quiet btn-sm" onClick={onCancel}>{annoNew ? "取消" : "关闭"}</button>
        {!annoNew && <button type="button" className="btn btn-ghost btn-sm" onClick={onDelete}>删除批注</button>}
        <button type="button" className="btn btn-accent btn-sm" onClick={onSave}>{annoNew ? "添加批注" : "更新批注"}</button>
      </div>
    </div>
  );
}

/* 改写弹层：自定义要求 / 调音 / 改写中 / 出错 / 候选（选中的那段字变了时不再原样替换，给复制）。
   selText 是送去改写的字（选了几段时一段一行）；每一版候选按段画，模型把几句对白放在同一段时标一句（仍可替换）。 */
export function WrRewritePop({ popRef, style, wide = false, phase, finding, selText, results, pick, onPick, custom, onCustom, tone, onTone, error, staleSel, copied, onCopy, onRun, onRetry, onDismiss, onReplace, onOpenSettings }) {
  const selParas = String(selText || "").split("\n").filter((part) => part.trim());
  const selCount = Array.from(String(selText || "").replace(/\n/g, "")).length;
  return (
    <div className={`wr-irw-pop ${wide ? "is-wide" : ""}`} ref={popRef} tabIndex={-1} role="dialog" aria-label="AI 改写选中的文字" style={style} onMouseDown={(e) => e.stopPropagation()}>
      <div className="wr-irw-head"><I.Sparkles size={14} /> AI 改写{phase === "result" && results.length > 1 ? `（${results.length} 版）` : ""}{finding ? ` · 按诊断：${finding.label || ""}` : ""} <span className="sp">选中 {selParas.length > 1 ? `${selParas.length} 段 · ` : ""}{selCount} 字</span></div>
      {phase === "custom" && (
        <div className="wr-irw-custom">
          <input className="wr-irw-input" autoFocus value={custom} aria-label="改写要求" placeholder="如：更冷一点、删掉比喻、加一个动作…"
            onChange={(e) => onCustom(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && e.keyCode !== 229 && custom.trim()) { e.preventDefault(); onRun(custom.trim()); } }} />
          <button type="button" className="btn btn-accent btn-sm" disabled={!custom.trim()} onClick={() => custom.trim() && onRun(custom.trim())}>改写</button>
        </div>
      )}
      {phase === "tune" && (
        <>
          <div className="wr-tune">
            <WrToneSlider label="语气" lo="冷峻" hi="温情" value={tone.warm} onChange={(v) => onTone({ warm: v })} />
            <WrToneSlider label="繁简" lo="凝练" hi="铺陈" value={tone.expand} onChange={(v) => onTone({ expand: v })} />
            <WrToneSlider label="显隐" lo="含蓄" hi="直白" value={tone.direct} onChange={(v) => onTone({ direct: v })} />
            <div className="wr-tune-prev">{wrToneInstr(tone)}</div>
          </div>
          <div className="wr-irw-foot">
            <button type="button" className="btn btn-quiet btn-sm" onClick={onDismiss}>取消</button>
            <button type="button" className="btn btn-accent btn-sm" onClick={() => onRun(wrToneInstr(tone))}>按这个语气改写</button>
          </div>
        </>
      )}
      {phase === "loading" && (<div className="wr-irw-load" role="status"><Spinner size={15} /> 正在改写，稍等…</div>)}
      {phase === "error" && (
        <>
          <div className="wr-irw-body">
            <WrAiErrorBlock error={error} onRetry={onRetry} onOpenSettings={onOpenSettings} />
          </div>
          <div className="wr-irw-foot"><button type="button" className="btn btn-quiet btn-sm" onClick={onDismiss}>关闭</button></div>
        </>
      )}
      {phase === "result" && (
        <>
          <div className="wr-irw-body">
            {staleSel && (
              <Notice tone="warn" className="wr-irw-stale"
                actions={<button type="button" className="btn btn-ghost btn-sm" onClick={onCopy}>{copied ? "已复制" : `复制第 ${pick + 1} 版`}</button>}>
                选中的那段字已经不在原处（改过、删掉，或正文刚重新载入），这一版没有替换进去。可以先复制这一版，或重新选中再改。
              </Notice>
            )}
            <div className="wr-irw-orig"><WrParagraphs paragraphs={selParas} /></div>
            <div className="wr-irw-cands" role="radiogroup" aria-label="改写版本">
              {results.map((result, i) => (
                <button type="button" key={i} role="radio" aria-checked={pick === i} className={`wr-irw-cand ${pick === i ? "is-sel" : ""}`} onClick={() => onPick(i)}>
                  <span className="wr-irw-cand-k" aria-hidden="true">{i + 1}</span>
                  <span className="wr-irw-cand-t">
                    <WrParagraphs paragraphs={result.paragraphs} />
                    {result.collapsed && <span className="wr-irw-cand-note">这一版把几句对白放在了同一段</span>}
                  </span>
                </button>
              ))}
            </div>
          </div>
          <div className="wr-irw-foot">
            <button type="button" className="btn btn-quiet btn-sm" onClick={onDismiss}>取消</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>再改一次</button>
            <button type="button" className="btn btn-accent btn-sm" disabled={staleSel} onClick={onReplace}>替换为第 {pick + 1} 版</button>
          </div>
        </>
      )}
    </div>
  );
}
