import React from "react";
import ReactDOM from "react-dom";
import { I } from "./icons.jsx";
import { WsWorks, useActiveWork, useWorks } from "./ws-works.jsx";
import { WsDialog, isImeComposing, topModalLayer, useFocusTrap } from "./ws-dialog.jsx";
import { wsConfirm } from "./ws-notify.jsx";

/* ==========================================================
   WorkSwitcher — 作品切换器（侧栏顶部的书名按钮）
   点开是一个书架浮层：每部作品、当前的打勾、迷你进度和「新建作品」。
   浮层与新建对话框都 portal 到 <body>，侧栏的 overflow / backdrop-filter 裁不到它们。
   键盘：打开后焦点落在当前作品上，↑↓ 在各行之间移动，Tab 不出浮层（焦点陷阱），
   Esc 只关浮层并把焦点还给书名按钮：浮层在最上层时于 window 的捕获阶段截住这一下，不再往下传——
   过去一下 Esc 同时关掉浮层和写作台底下的续写托盘 / 沉浸写作。浮层上叠着删除确认框时它不在最上层，
   Esc 归确认框。
   删除一部作品先经应用内确认框（ws-notify.jsx 的 wsConfirm），不再弹浏览器的 confirm。
   纯 ESM，不写 window。
   ========================================================== */

const { useState, useEffect, useRef } = React;

const WS_ACCENTS = [
  { id: "crimson", label: "潮红" },
  { id: "gold", label: "暮金" },
  { id: "sage", label: "苔绿" },
  { id: "slate", label: "石青" },
];

function WorkSwitcher({ go }) {
  const works = useWorks();
  const active = useActiveWork();
  const [open, setOpen] = useState(false);
  const [newOpen, setNewOpen] = useState(false);
  const brandRef = useRef(null);

  const focusBrand = () => { if (brandRef.current) brandRef.current.focus({ preventScroll: true }); };

  // 命令面板 / 空书架页可以直接要新建对话框
  useEffect(() => {
    const h = () => { setOpen(false); setNewOpen(true); };
    window.addEventListener("ws:new-work", h);
    return () => window.removeEventListener("ws:new-work", h);
  }, []);

  // 新建对话框关掉后把焦点还给书名按钮：放在 effect 里，排在 WsDialog 卸载时的焦点归还之后
  // （它记下的「打开前焦点」是已经随浮层消失的「新建作品」按钮，会落回 body）。
  const newWasOpen = useRef(false);
  useEffect(() => {
    if (newWasOpen.current && !newOpen) focusBrand();
    newWasOpen.current = newOpen;
  }, [newOpen]);

  const closePopover = () => { setOpen(false); focusBrand(); };
  const pickWork = (id) => { WsWorks.setActive(id); setOpen(false); focusBrand(); go("home"); };
  const startNew = () => { setOpen(false); setNewOpen(true); };
  const closeNew = () => setNewOpen(false);
  const createWork = (data) => { WsWorks.create(data); setNewOpen(false); go("home"); };

  return (
    <>
      <button ref={brandRef} type="button" className={`ws-brand ${open ? "is-open" : ""}`} data-testid="work-switcher" onClick={() => setOpen(o => !o)}
        aria-haspopup="dialog" aria-expanded={open} title="切换 / 新建作品">
        <span className="ws-brand-mark" data-accent={active.accent}>{active.mark}</span>
        <span className="ws-brand-text">
          <span className="ws-brand-title">{active.title}</span>
          <span className="ws-brand-sub">{active.genre}</span>
        </span>
        <span className="ws-brand-caret" aria-hidden="true"><I.ChevronDown size={15} /></span>
      </button>

      {open && ReactDOM.createPortal(
        <WorkPopover works={works} activeId={active.id} onPick={pickWork} onNew={startNew} onClose={closePopover} />,
        document.body,
      )}
      {newOpen && <NewWorkModal onCreate={createWork} onClose={closeNew} />}
    </>
  );
}

function workProgressLine(w) {
  if (!(w.chaptersWritten > 0)) return "尚未开始";
  const pct = w.wordsTarget ? Math.min(100, Math.round((w.wordsTotal / w.wordsTarget) * 100)) : 0;
  return `${(w.wordsTotal / 10000).toFixed(1)} 万字 · ${pct}%`;
}

function WorkPopover({ works, activeId, onPick, onNew, onClose }) {
  const panelRef = useRef(null);
  const activeRowRef = useRef(null);

  // 打开时焦点落在当前作品那一行（没有就落在第一行）；Tab 在浮层里循环。
  // 关上时由 onClose / onPick 把焦点送回书名按钮，陷阱自己不还焦点（「新建作品」要把焦点交给新建对话框）。
  useFocusTrap(panelRef, true, { initialFocus: activeRowRef, restoreFocus: false });

  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "Escape" || isImeComposing(e) || topModalLayer() !== panelRef.current) return;
      e.preventDefault();
      e.stopPropagation();
      closeRef.current();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  const onKeyDown = (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    const nodes = [...(panelRef.current ? panelRef.current.querySelectorAll(".ws-wsw-row, .ws-wsw-new") : [])];
    if (!nodes.length) return;
    e.preventDefault();
    const current = nodes.indexOf(document.activeElement);
    let next = 0;
    if (e.key === "End") next = nodes.length - 1;
    else if (e.key === "ArrowDown") next = current < 0 ? 0 : (current + 1) % nodes.length;
    else if (e.key === "ArrowUp") next = current <= 0 ? nodes.length - 1 : current - 1;
    nodes[next].focus();
  };

  const removeWork = async (e, w) => {
    e.stopPropagation();
    const ok = await wsConfirm({
      title: `删除《${w.title}》？`,
      body: "这部作品会连同章节、构思、正文和待办一起进入回收站，可以在「回收站」里整体恢复。",
      confirmLabel: "移到回收站",
      tone: "danger",
    });
    if (ok) WsWorks.remove(w.id);
  };

  return (
    <div className="ws-wsw-scrim" onClick={onClose}>
      <div ref={panelRef} className="ws-wsw" role="dialog" aria-modal="true" aria-label="切换作品" tabIndex={-1}
        onClick={(e) => e.stopPropagation()} onKeyDown={onKeyDown}>
        <div className="ws-wsw-head">
          <span className="ws-wsw-head-lbl">作品</span>
          <span className="ws-wsw-head-n">{works.length} 部</span>
        </div>
        <div className="ws-wsw-list">
          {works.map(w => {
            const pct = w.wordsTarget ? Math.min(100, Math.round((w.wordsTotal / w.wordsTarget) * 100)) : 0;
            const isActive = w.id === activeId;
            const deletable = !WsWorks.isSeed(w.id) && works.length > 1;
            return (
              <div key={w.id} className="ws-wsw-item">
                <button type="button" ref={isActive ? activeRowRef : undefined} className={`ws-wsw-row ${isActive ? "is-active" : ""}`}
                  aria-current={isActive ? "true" : undefined} onClick={() => onPick(w.id)}>
                  <span className="ws-wsw-mark" data-accent={w.accent}>{w.mark}</span>
                  <span className="ws-wsw-meta">
                    <span className="ws-wsw-title">{w.title}</span>
                    <span className="ws-wsw-sub">{w.genre}</span>
                    <span className="ws-wsw-progress">{workProgressLine(w)}</span>
                    <span className="ws-wsw-bar" aria-hidden="true"><i data-accent={w.accent} style={{ width: pct + "%" }} /></span>
                  </span>
                  {isActive && <span className="ws-wsw-check" aria-hidden="true"><I.Check size={15} /></span>}
                </button>
                {deletable && (
                  <button type="button" className="ws-wsw-del" title="删除这部作品" aria-label={`删除《${w.title}》`}
                    onClick={(e) => { void removeWork(e, w); }}>
                    <I.Trash size={14} />
                  </button>
                )}
              </div>
            );
          })}
        </div>
        <button type="button" className="ws-wsw-new" data-testid="work-new-open" onClick={onNew}>
          <span className="ws-wsw-new-ic"><I.Plus size={16} /></span> 新建作品
        </button>
      </div>
    </div>
  );
}

function NewWorkModal({ onCreate, onClose }) {
  const [title, setTitle] = useState("");
  const [genre, setGenre] = useState("");
  const [sub, setSub] = useState("");
  const [target, setTarget] = useState(100000);
  const [accent, setAccent] = useState("slate");
  const ref = useRef(null);
  const canCreate = title.trim().length > 0;
  const submit = () => { if (canCreate) onCreate({ title, genre, sub, wordsTarget: target, accent }); };

  return (
    <WsDialog onClose={onClose} labelledBy="ws-nw-title" size="md" className="ws-nw" initialFocus={ref}>
      <div className="ws-nw-head">
        <span className="ws-nw-mark" data-accent={accent} aria-hidden="true">{Array.from(title.trim())[0] || "新"}</span>
        <h2 className="ws-nw-title" id="ws-nw-title">新建作品</h2>
        <button type="button" className="ws-nw-x" onClick={onClose} aria-label="取消新建"><I.X size={18} /></button>
      </div>

      <div className="ws-nw-body">
        <label className="ws-nw-field">
          <span className="ws-nw-lbl">书名 <em>必填</em></span>
          <input ref={ref} className="ws-nw-input" data-testid="work-new-title" value={title} placeholder="例如：你的书名"
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !isImeComposing(e)) submit(); }} />
        </label>
        <label className="ws-nw-field">
          <span className="ws-nw-lbl">题材</span>
          <input className="ws-nw-input" value={genre} placeholder="例如：悬疑长篇"
            onChange={(e) => setGenre(e.target.value)} />
        </label>
        <label className="ws-nw-field">
          <span className="ws-nw-lbl">一句话简介</span>
          <textarea className="ws-nw-input ws-nw-area" data-testid="work-new-synopsis" value={sub} placeholder="用一句话说清这部作品是关于什么的——也可以之后在雪花里再写。"
            rows={2} onChange={(e) => setSub(e.target.value)} />
        </label>
        <div className="ws-nw-row2">
          <label className="ws-nw-field">
            <span className="ws-nw-lbl">目标字数</span>
            <div className="ws-nw-target">
              <input className="ws-nw-input" type="number" min="10000" step="10000" value={target}
                onChange={(e) => setTarget(e.target.value)} />
              <span className="ws-nw-target-suffix">字</span>
            </div>
          </label>
          <div className="ws-nw-field">
            <span className="ws-nw-lbl" id="ws-nw-accent-lbl">主色</span>
            <div className="ws-nw-accents" role="group" aria-labelledby="ws-nw-accent-lbl">
              {WS_ACCENTS.map(a => (
                <button key={a.id} type="button" title={a.label} aria-label={a.label} aria-pressed={accent === a.id}
                  className={`ws-nw-acc ${accent === a.id ? "is-sel" : ""}`} data-accent={a.id}
                  onClick={() => setAccent(a.id)} />
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="ws-nw-foot">
        <button type="button" className="btn btn-ghost" onClick={onClose}>取消</button>
        <button type="button" className="btn btn-accent" data-testid="work-new-submit" disabled={!canCreate} onClick={submit}>
          <I.Plus size={15} /> 创建并进入
        </button>
      </div>
    </WsDialog>
  );
}

export { WorkSwitcher, WorkPopover, NewWorkModal, WS_ACCENTS };
