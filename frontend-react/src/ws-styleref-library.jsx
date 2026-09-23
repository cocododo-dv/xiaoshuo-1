import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, Spinner, Tag } from "./ws-ui.jsx";
import { getOperatorRef } from "./lib/client.js";
import {
  SR_CLOUD_POLICIES, SR_RIGHTS_TERMS, srAppliedToWork, srBookPipeline, srFilterBooks, srPolicyNeedsSendRights,
  srRightsReady, srSortBooks,
} from "./ws-styleref-model.js";
import {
  srBooks, srBooksState, srDeleteBooks, srLoadRuntime, srRunImport, srRuntime,
} from "./ws-styleref-store.js";
import { SrActivityPanel, srRunningFor } from "./ws-styleref-activity.jsx";
import { SrErrorLine, srActiveWork, srNotify, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 参考书库（左栏；≤1280 收进页头「参考书库」对话框）与导入
   · srPipelineFor：一本书现在走到哪一步（书库徽标、页头共用同一个说法）
   · srConfirmDeleteBooks：删书前的确认（列出书名、说清丢什么、哪部作品正在用）；单本删除也走它
   · SrLibrary：书库标题 + 导入、参考书活动、筛选、书单（「选择」后可多选删除）
   · SrImportDialog：导入对话框——原文能发到哪里（三档，默认跟着当前模型）、权属声明、文件、书名作者；
     出错就地说清楚（同一份文本 →「打开这本」；没有模型 →「去设置模型」）
   ========================================================== */

export function srPipelineFor(book) {
  if (!book) return null;
  const work = srActiveWork();
  return srBookPipeline(book, { running: srRunningFor(book.id), workId: work ? work.id : null });
}

/* 删书前确认：返回 true 才删。书名最多列 8 本；正在用于当前作品的书单独点出来。 */
export async function srConfirmDeleteBooks(books) {
  const list = (books || []).filter(Boolean);
  if (!list.length) return false;
  const work = srActiveWork();
  const shown = list.slice(0, 8).map((b) => `《${b.title}》`).join("、");
  const more = list.length > 8 ? ` 等 ${list.length} 本` : "";
  const inUse = work ? list.filter((b) => srAppliedToWork(b, work.id)) : [];
  const usage = inUse.length
    ? `《${work.title || "当前作品"}》正在用${inUse.map((b) => `《${b.title}》`).join("、")}的文风，删除后起草时不再带它。`
    : "";
  return wsConfirm({
    title: list.length === 1 ? `删除参考书《${list[0].title}》？` : `删除 ${list.length} 本参考书？`,
    body: `${list.length > 1 ? `${shown}${more}。` : ""}每本书的原文、文风画像、学习记录、样例窗口与禁用词会一并删除。${usage}此操作无法恢复。`,
    confirmLabel: list.length === 1 ? "删除这本书" : `删除 ${list.length} 本`,
    tone: "danger",
  });
}

/* 左栏（和窄屏下的书库对话框）：书库标题 + 导入、参考书活动、筛选、书单；「选择」后多选删除。 */
export function SrLibrary({ bookId, onSelect, onImport, onDeleted }) {
  useSrStore("books", "activity");
  const [query, setQuery] = React.useState("");
  const [selecting, setSelecting] = React.useState(false);
  const [selected, setSelected] = React.useState(() => new Set());
  const [busy, setBusy] = React.useState(false);
  const all = srBooks();
  const work = srActiveWork();
  const sorted = srSortBooks(all, work ? work.id : null);
  const showFilter = all.length > 8;
  const books = showFilter ? srFilterBooks(sorted, query) : sorted;
  const { phase, error, code } = srBooksState();

  /* 书没了（删掉 / 别处删的）就从选择里拿掉 */
  React.useEffect(() => {
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => all.some((b) => b.id === id)));
      return next.size === prev.size ? prev : next;
    });
  }, [all]);

  const toggle = (id) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const stopSelecting = () => { setSelecting(false); setSelected(new Set()); };
  const allVisibleSelected = books.length > 0 && books.every((b) => selected.has(b.id));

  const deleteSelected = async () => {
    const chosen = all.filter((b) => selected.has(b.id));
    if (!chosen.length || busy) return;
    if (!(await srConfirmDeleteBooks(chosen))) return;
    setBusy(true);
    try {
      const result = await srDeleteBooks(chosen.map((b) => b.id));
      const failed = (result.results || []).filter((item) => !item.deleted && !(item.error && item.error.code === "STYLE_REFERENCE_BOOK_NOT_FOUND"));
      const done = chosen.length - failed.length;
      if (failed.length) srNotify(`删除了 ${done} 本，另有 ${failed.length} 本没删成：${failed.map((item) => (item.error && item.error.message) || item.book_id).join("；")}`);
      else srNotify(`已删除 ${done} 本参考书`, "neutral");
      stopSelecting();
      if (onDeleted) onDeleted(chosen.map((b) => b.id));
    } catch (e) {
      srNotifyError(e, "删除没有完成，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sr-lib">
      <header className="sr-books-head">
        <h2 className="sr-books-title text-serif">参考书库</h2>
        <div className="sr-books-head-actions">
          {all.length > 0 && !selecting && (
            <button type="button" className="btn btn-quiet btn-sm" data-testid="sr-books-select" onClick={() => setSelecting(true)}>选择</button>
          )}
          <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-books-import" onClick={onImport}><I.Plus size={13} /> 导入参考书</button>
        </div>
      </header>

      <SrActivityPanel onOpenBook={onSelect} />

      {showFilter && (
        <div className="sr-book-filter">
          <I.Search size={13} />
          <input className="input" type="search" placeholder="按书名或作者筛选" aria-label="筛选参考书" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      )}

      {selecting && (
        <div className="sr-select-bar" data-testid="sr-select-bar">
          <label className="sr-select-all">
            <input
              type="checkbox"
              checked={allVisibleSelected}
              aria-label="全选"
              onChange={() => setSelected(allVisibleSelected ? new Set() : new Set(books.map((b) => b.id)))}
            />
            <span>已选 <b className="tab-num">{selected.size}</b> 本</span>
          </label>
          <button type="button" className="btn btn-danger btn-sm" data-testid="sr-books-delete-selected" disabled={!selected.size || busy} onClick={deleteSelected}>
            {busy ? <><Spinner size={12} /> 删除中…</> : <><I.Trash size={13} /> 删除所选</>}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={stopSelecting}>完成</button>
        </div>
      )}

      {phase === "loading" && !all.length ? (
        <ul className="sr-book-list" aria-busy="true" aria-label="正在读取参考书库">
          {[0, 1, 2].map((i) => <li key={i} className="sr-book-skel" aria-hidden="true"><span /><span /></li>)}
        </ul>
      ) : phase === "error" && !all.length ? (
        <p className="sr-lib-empty sr-lib-error" role="status" title={[error, code && `错误代码：${code}`].filter(Boolean).join("\n")}>
          <I.AlertTriangle size={13} aria-hidden="true" /> 读不到书库
        </p>
      ) : !all.length ? (
        <p className="sr-lib-empty">还没有参考书。导入一本你想学它文风的书。</p>
      ) : (
        <ul className="sr-book-list">
          {books.map((b) => (
            <SrBookRow
              key={b.id}
              book={b}
              active={bookId === b.id}
              selecting={selecting}
              checked={selected.has(b.id)}
              onSelect={onSelect}
              onToggle={toggle}
            />
          ))}
          {showFilter && books.length === 0 && <li className="sr-lib-empty">没有书名或作者含「{query}」的参考书。</li>}
        </ul>
      )}
    </div>
  );
}

/* 书单一行：书脊色、书名、万字、进度徽标；选择模式下前面有勾选框，点整行就是勾选。 */
function SrBookRow({ book: b, active, selecting, checked, onSelect, onToggle }) {
  const p = srPipelineFor(b);
  const wan = (b.chars / 10000).toFixed(b.chars >= 100000 ? 0 : 1);
  const label = `${b.title}${b.author ? ` · ${b.author}` : ""} · ${b.chars.toLocaleString()} 字`;
  if (selecting) {
    return (
      <li className={`sr-book-item${checked ? " is-checked" : ""}`}>
        <label className="sr-book sr-book-check" title={label}>
          <input type="checkbox" checked={checked} data-sr-select={b.id} aria-label={`选择《${b.title}》`} onChange={() => onToggle(b.id)} />
          <span className="sr-book-body">
            <span className="sr-book-title text-serif">{b.title}</span>
            <span className="sr-book-meta">{b.author ? `${b.author} · ` : ""}{wan} 万字</span>
          </span>
          {p && <Tag tone={p.tone} className="sr-book-chip">{p.label}</Tag>}
        </label>
      </li>
    );
  }
  return (
    <li className="sr-book-item">
      <button
        type="button"
        className={`sr-book ${active ? "is-active" : ""}`}
        aria-current={active ? "true" : undefined}
        data-sr-book={b.id}
        title={label}
        onClick={() => onSelect(b.id)}
      >
        <span className={`sr-book-spine spine-${b.color}`} aria-hidden="true" />
        <span className="sr-book-body">
          <span className="sr-book-title text-serif">{b.title}</span>
          <span className="sr-book-meta">{b.author ? `${b.author} · ` : ""}{wan} 万字</span>
        </span>
        {p && <Tag tone={p.tone} className="sr-book-chip">{p.label}</Tag>}
      </button>
    </li>
  );
}

/* ==========================================================
   导入
   ========================================================== */

/* 与后端 ingest 的后缀白名单一致：只接受纯文本 / Markdown。 */
const SR_IMPORT_ACCEPT = ".txt,.md,.markdown";
function srFileTitle(name) { return String(name || "").replace(/\.[^.]+$/, "").trim(); }

function srBuildRightsDeclaration(cloudPolicy, analysis, send) {
  return {
    declared: true,
    analysis_rights: analysis === true,
    send_rights: srPolicyNeedsSendRights(cloudPolicy) && send === true,
    declared_by: getOperatorRef(),
  };
}

/* 为什么现在不能导入（有原因就锁住「导入」并把原因写在旁边） */
function srImportBlocker({ runtime, policy, rightsReady, file, title }) {
  if (runtime && runtime.llm_enabled === false) return "先接入模型：导入之后要用模型给每一段分类";
  if (policy === "local_only" && runtime && runtime.llm_enabled && runtime.llm_is_local === false) {
    return "现在给段落分类的是云端模型，「仅本机模型」的书导入不了";
  }
  if (!rightsReady) return "先确认权属声明";
  if (!file) return "还没选文件";
  if (!title.trim()) return "还没填书名";
  return null;
}

export function SrImportDialog({ open, onClose, onImported, onOpenBook, onOpenSettings }) {
  useSrStore("detail");
  const runtimeState = srRuntime();
  const runtime = runtimeState.phase === "ready" ? runtimeState.data : null;
  const [policy, setPolicy] = React.useState(null);
  const [analysisRights, setAnalysisRights] = React.useState(false);
  const [sendRights, setSendRights] = React.useState(false);
  const [file, setFile] = React.useState(null);
  const [title, setTitle] = React.useState("");
  const [titleTouched, setTitleTouched] = React.useState(false);
  const [author, setAuthor] = React.useState("");
  const [fileError, setFileError] = React.useState(null);
  const [dragOver, setDragOver] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const titleId = React.useId();
  const descId = React.useId();
  const fileInputId = React.useId();

  React.useEffect(() => {
    if (!open) return;
    setPolicy(null);
    setAnalysisRights(false);
    setSendRights(false);
    setFile(null);
    setTitle("");
    setTitleTouched(false);
    setAuthor("");
    setFileError(null);
    setDragOver(false);
    setBusy(false);
    setError(null);
    srLoadRuntime({ force: true });
  }, [open]);

  /* 默认范围跟着当前模型走：分类走本机模型时「仅本机模型」，否则「可发送全文」（云端模型下默认仅本机必然导入失败）；
     读不到运行时就先按最私密的一档，作者可以改。作者自己选过就不再覆盖。 */
  const effectivePolicy = policy || (runtime && runtime.default_cloud_policy) || "local_only";
  const needsSend = srPolicyNeedsSendRights(effectivePolicy);
  const rights = { analysis_rights: analysisRights, send_rights: needsSend && sendRights };
  const rightsReady = srRightsReady(effectivePolicy, rights);
  const blocker = srImportBlocker({ runtime, policy: effectivePolicy, rightsReady, file, title });

  const takeFile = (f) => {
    if (!f) return;
    if (!/\.(txt|md|markdown)$/i.test(f.name || "")) {
      setFileError("只能导入纯文本（.txt）或 Markdown（.md）文件。");
      return;
    }
    setFileError(null);
    setError(null);
    setFile(f);
    if (!titleTouched || !title.trim()) setTitle(srFileTitle(f.name));
  };

  const submit = async () => {
    if (blocker || busy) return;
    setBusy(true);
    setError(null);
    try {
      const data = await srRunImport({
        file,
        title: title.trim(),
        authorLabel: author.trim() || null,
        cloudPolicy: effectivePolicy,
        rightsDeclaration: srBuildRightsDeclaration(effectivePolicy, analysisRights, sendRights),
      });
      setBusy(false);
      if (onImported) onImported(data);
    } catch (e) {
      setBusy(false);
      setError(e);
    }
  };

  const onErrorAction = (action) => {
    if (action.type === "open_book" && onOpenBook) onOpenBook(action.bookId);
    else if (action.type === "settings" && onOpenSettings) onOpenSettings();
  };

  return (
    <WsDialog open={open} onClose={onClose} labelledBy={titleId} describedBy={descId} size="lg" className="sr-import-dialog" portal={false}>
      <header className="ws-dialog-head">
        <div>
          <h2 id={titleId} className="ws-dialog-title text-serif">导入参考书</h2>
          <p id={descId} className="ws-dialog-desc">
            先定这本书的原文能发到哪里，再选文件。这个选择随书保存，之后分类、学习、起草都按它来；系统只学写法，不复刻原文、人物或桥段。
          </p>
        </div>
        <button type="button" className="ws-dialog-x" aria-label="关闭导入" onClick={onClose}><I.X size={16} /></button>
      </header>
      <div className="ws-dialog-body sr-import-body">
        {runtime && runtime.llm_enabled === false && (
          <Notice
            tone="warn"
            testId="sr-import-no-llm"
            actions={onOpenSettings ? <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenSettings}>去设置模型</button> : null}
          >
            还没有接入模型。导入之后要由模型给每一段分类（导入就会开始），没有模型就导入不了。
          </Notice>
        )}
        <fieldset className="sr-policy-list" data-testid="sr-import-policy">
          <legend className="sr-policy-legend">原文能发到哪里</legend>
          {SR_CLOUD_POLICIES.map((item) => (
            <label key={item.id} className={`sr-policy ${effectivePolicy === item.id ? "is-selected" : ""}`}>
              <input type="radio" name="sr-cloud-policy" value={item.id} checked={effectivePolicy === item.id} onChange={() => { setPolicy(item.id); setError(null); }} />
              <span className="sr-policy-mark" aria-hidden="true" />
              <span className="sr-policy-copy">
                <span className="sr-policy-title">
                  {item.label}
                  {item.hint && <em>{item.hint}</em>}
                  {runtime && runtime.default_cloud_policy === item.id && <em className="is-default">按当前模型推荐</em>}
                </span>
                <span className="sr-policy-detail">{item.detail}</span>
              </span>
            </label>
          ))}
        </fieldset>
        {effectivePolicy === "local_only" && runtime && runtime.llm_enabled && runtime.llm_is_local === false && (
          <Notice tone="warn" testId="sr-import-local-blocked" actions={onOpenSettings ? <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenSettings}>去设置模型</button> : null}>
            现在给段落分类的是云端模型，「仅本机模型」的书会被拒绝：先在设置里把段落分类换成本机模型，或选另外两档。
          </Notice>
        )}
        <fieldset className="sr-rights-list">
          <legend className="sr-policy-legend">权属声明</legend>
          <label className={`sr-rights ${analysisRights ? "is-checked" : ""}`}>
            <input type="checkbox" data-testid="sr-rights-analysis" checked={analysisRights} onChange={(event) => setAnalysisRights(event.target.checked)} />
            <span>{SR_RIGHTS_TERMS.analysis}</span>
          </label>
          {needsSend && (
            <label className={`sr-rights ${sendRights ? "is-checked" : ""}`}>
              <input type="checkbox" data-testid="sr-rights-send" checked={sendRights} onChange={(event) => setSendRights(event.target.checked)} />
              <span>{SR_RIGHTS_TERMS.send}</span>
            </label>
          )}
        </fieldset>

        <fieldset className="sr-import-file-set">
          <legend className="sr-policy-legend">文件</legend>
          <label
            htmlFor={fileInputId}
            className={`sr-import-drop${dragOver ? " is-over" : ""}${file ? " has-file" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => { e.preventDefault(); setDragOver(false); takeFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]); }}
          >
            <I.FileInput size={18} />
            {file
              ? <span className="sr-import-file-name"><b>{file.name}</b><em>{(file.size / 1024).toFixed(file.size > 102400 ? 0 : 1)} KB · 点这里换一个</em></span>
              : <span className="sr-import-file-name"><b>选择或拖入文件</b><em>纯文本 .txt 或 Markdown .md</em></span>}
          </label>
          <input
            id={fileInputId}
            className="ws-sr-only"
            type="file"
            accept={SR_IMPORT_ACCEPT}
            data-testid="sr-import-file"
            onChange={(e) => { takeFile(e.target.files && e.target.files[0]); e.target.value = ""; }}
          />
          {fileError && <p className="sr-rights-hint" role="alert">{fileError}</p>}
        </fieldset>

        <div className="sr-import-fields">
          <label className="sr-import-field">
            <span className="label">书名</span>
            <input className="input" data-testid="sr-import-title" value={title} maxLength={200} placeholder="选了文件后按文件名填入" onChange={(e) => { setTitle(e.target.value); setTitleTouched(true); }} />
          </label>
          <label className="sr-import-field">
            <span className="label">作者 <em className="sr-optional">可不填</em></span>
            <input className="input" data-testid="sr-import-author" value={author} maxLength={120} placeholder="书库里显示" onChange={(e) => setAuthor(e.target.value)} />
          </label>
        </div>
        <SrErrorLine error={error} onAction={onErrorAction} testId="sr-import-error" />
      </div>
      <footer className="ws-dialog-foot sr-import-foot">
        {blocker && <span className="sr-import-why" data-testid="sr-import-why">{blocker}</span>}
        <button type="button" className="btn btn-ghost" onClick={onClose}>取消</button>
        <button type="button" className="btn btn-accent" data-testid="sr-import-submit" disabled={!!blocker || busy} onClick={submit}>
          {busy ? <><Spinner size={13} /> 导入中…</> : <><I.FileInput size={14} /> 导入</>}
        </button>
      </footer>
    </WsDialog>
  );
}
