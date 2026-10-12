import React from "react";
import { WsDialog } from "./ws-dialog.jsx";
import { CloseButton, Notice } from "./ws-ui.jsx";
import { chapterLabel } from "./labels/catalog.js";

/* ==========================================================
   成稿中心的两个流转对话框：批准终稿 / 重新打开（「退回小修」2026-10 随送审一步去掉）。
   都走 WsDialog（焦点陷阱、Esc 与遮罩走同一个关闭、关闭后焦点回到按钮、夜间遮罩用 --scrim）。
   忙碌时不许关（onBeforeClose 拦下）：否则作者会以为操作取消了，实际服务端还在做。

   外壳常驻、只切 open：页面挂载时两个 WsDialog 就在（open=false 时什么都不渲染），打开只是
   把 open 翻成 true。开发模式（React.StrictMode）会把「新挂载」的 effect 连跑两遍，
   WsDialog 的焦点陷阱在第二遍里会把第一遍自己聚焦的输入框记成「打开它的元素」，
   结果焦点停在遮罩后面的按钮上、关闭后掉到 body；常驻外壳的 effect 只是依赖变化，不会被连跑。
   表单状态放在只在打开时挂载的内层组件里，所以每次打开都是一张空表。
   ========================================================== */

const { useId, useRef, useState } = React;

/* 「第 3 章 · 盐场」；章名就是「第 3 章」这种占位时只写一次 */
const chapterTitle = (picked) => chapterLabel(picked, { maxTitle: 30 });

/* 常驻外壳：只管开关、标题关联与忙碌时不许关。children 是 ({ titleId, descId }) => 内层表单。 */
function ManuDialog({ open, busy, onClose, initialFocus, children }) {
  const titleId = useId();
  const descId = useId();
  return (
    <WsDialog open={open} onClose={onClose} onBeforeClose={() => !busy} labelledBy={titleId} describedBy={descId}
      initialFocus={initialFocus} size="md" className="ms-dialog">
      {() => children({ titleId, descId })}
    </WsDialog>
  );
}

/* 两个对话框共用的版式：标题、说明、关闭按钮、错误、取消 + 确认 */
function ManuDialogFrame({ titleId, descId, title, desc, busy, error, onClose, confirm, children }) {
  const close = () => { if (!busy) onClose(); };
  return (
    <>
      <div className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title text-serif" id={titleId}>{title}</h2>
          <p className="ws-dialog-desc" id={descId}>{desc}</p>
        </div>
        <CloseButton onClick={close} className="ws-dialog-x" label={busy ? "处理中，暂时不能关闭" : "关闭"} />
      </div>
      <div className="ws-dialog-body ms-dialog-body">
        {children}
        {error && <Notice tone="danger">{error}</Notice>}
      </div>
      <div className="ws-dialog-foot">
        <button type="button" className="btn btn-ghost" disabled={busy} onClick={close}>取消</button>
        {confirm}
      </div>
    </>
  );
}

/* 批准终稿：通读确认绑定当前服务端正文哈希。canonPending：有终稿、正史还没核对的场数——只提一句，不拦批准 */
function ManuApprovalDialog({ open = true, ...props }) {
  const focusRef = useRef(null);
  return (
    <ManuDialog open={open} busy={props.busy} onClose={props.onClose} initialFocus={focusRef}>
      {(ids) => <ManuApprovalForm {...props} {...ids} focusRef={focusRef} />}
    </ManuDialog>
  );
}

function ManuApprovalForm({ titleId, descId, focusRef, picked, canonPending = 0, busy, error, onClose, onConfirm }) {
  const [read, setRead] = useState(false);
  const [readNote, setReadNote] = useState("");
  const [revisionNotes, setRevisionNotes] = useState("");
  return (
    <ManuDialogFrame titleId={titleId} descId={descId} busy={busy} error={error} onClose={onClose}
      title={`批准终稿：${chapterTitle(picked)}`}
      desc="确认会绑定当前服务端正文哈希；正文若变化，必须重新通读确认。"
      confirm={(
        <button type="button" className="btn btn-accent" data-testid="approve-final-confirm" disabled={!read || busy}
          onClick={() => onConfirm({ readNote: readNote.trim(), revisionNotes: revisionNotes.trim() })}>
          {busy ? "服务端批准中…" : "确认通读并批准"}
        </button>
      )}>
      <label className="ms-dialog-check is-strong">
        <input ref={focusRef} data-testid="approve-read-confirm" type="checkbox" checked={read} disabled={busy} onChange={(e) => setRead(e.target.checked)} />
        我已从头到尾通读当前正文，并确认它可以成为终稿
      </label>
      {canonPending > 0 && (
        <p className="ms-dialog-note" data-testid="approve-canon-pending">{canonPending} 场正史未核对，不影响定稿（AI 抽出的事实照样用于后面各章的起草，抽错的可以随时在「正史」里驳回）。</p>
      )}

      <label className="ms-dialog-field">
        <span className="ms-dialog-k">通读备注 <small>可选</small></span>
        <textarea className="textarea ms-dialog-area" rows={2} maxLength={1000} value={readNote} disabled={busy}
          placeholder="记录通读时重点核对了什么。"
          onChange={(e) => setReadNote(e.target.value)} />
      </label>

      <label className="ms-dialog-field">
        <span className="ms-dialog-k">终稿备注 <small>可选</small></span>
        <textarea className="textarea ms-dialog-area" rows={2} maxLength={2000} value={revisionNotes} disabled={busy}
          placeholder="留给下一章或后续修订的提醒。"
          onChange={(e) => setRevisionNotes(e.target.value)} />
      </label>
    </ManuDialogFrame>
  );
}

/* 重新打开终稿：有理由、可审计，服务端级联撤销其后的批准 */
function ManuReopenDialog({ open = true, ...props }) {
  const focusRef = useRef(null);
  return (
    <ManuDialog open={open} busy={props.busy} onClose={props.onClose} initialFocus={focusRef}>
      {(ids) => <ManuReopenForm {...props} {...ids} focusRef={focusRef} />}
    </ManuDialog>
  );
}

function ManuReopenForm({ titleId, descId, focusRef, picked, busy, error, onClose, onConfirm }) {
  const [reason, setReason] = useState("");
  const can = reason.trim().length > 0 && reason.length <= 1000;
  return (
    <ManuDialogFrame titleId={titleId} descId={descId} busy={busy} error={error} onClose={onClose}
      title={`重新打开：${chapterTitle(picked)}`}
      desc="该章及其后已批准章节会失效，项目从本章重新推进；服务端会保留完整审计。"
      confirm={(
        <button type="button" className="btn btn-danger" data-testid="reopen-final-confirm" disabled={!can || busy} onClick={() => onConfirm({ reason: reason.trim() })}>
          {busy ? "重新打开中…" : "撤销批准并重新打开"}
        </button>
      )}>
      <label className="ms-dialog-field">
        <span className="ms-dialog-k">重新打开原因 <em>必填</em></span>
        <textarea ref={focusRef} className="textarea ms-dialog-area" rows={3} maxLength={1000} value={reason} disabled={busy}
          placeholder="说明为什么必须打破终稿锁；这段原因会进入审计记录。"
          onChange={(e) => setReason(e.target.value)} />
      </label>
    </ManuDialogFrame>
  );
}

export { ManuApprovalDialog, ManuReopenDialog };
