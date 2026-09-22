import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { Spinner, Tag } from "./ws-ui.jsx";
import { getOperatorRef } from "./lib/client.js";
import { SR_ACTIVITY_WHERE, srBindingActive, srBookPipeline, srFilterBooks, srSortBooks } from "./ws-styleref-model.js";
import {
  srBookApplied, srBooks, srBooksState, srDeepFor, srMeta, srProfilesOf, srRunImport, srSyncBooks,
} from "./ws-styleref-store.js";
import { SrActivityPanel, srRunningFor } from "./ws-styleref-activity.jsx";
import { srActiveWork, srNotify, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 参考书库（左栏；≤1280 收进页头「参考书库」对话框）与导入
   · srPipelineFor：一本书现在走到哪一步（书库徽标、页头共用同一个说法）
   · srLossSummary：删除 / 重新分类会丢掉什么（确认框正文）
   · SrLibrary：书库标题 + 导入、参考书活动、筛选、书单
   · SrImportDialog + srImportBook：导入单子（数据范围 + 权属声明 + 文件 + 书名作者）与发起导入
   ========================================================== */

/* 一本书现在走到哪一步 */
export function srPipelineFor(book) {
  if (!book) return null;
  return srBookPipeline(book, {
    profiles: srProfilesOf(book.id),
    applied: srBookApplied(book.id),
    deep: srDeepFor(book.id),
    running: srRunningFor(book.id),
  });
}

/* 「删除 / 重新分类会丢掉什么」：画像份数、正在用它的作品绑定、回测报告、禁用词。 */
export function srLossSummary(book, lead) {
  const deep = srDeepFor(book.id);
  const profiles = srProfilesOf(book.id) || (deep && deep.profile ? [deep.profile] : []);
  const bindings = (deep && Array.isArray(deep.bindings) ? deep.bindings : []).filter(srBindingActive);
  const parts = ["抽取结果与证据引文"];
  if (profiles.length) parts.push(`风格画像 ${profiles.length} 份`);
  if (bindings.length) parts.push(`应用绑定 ${bindings.length} 个`);
  parts.push("回测报告和禁用词");
  const work = srActiveWork();
  const tail = srBookApplied(book.id) && work
    ? `《${work.title || "当前作品"}》正在用这本书的风格，删除后起草时不再带它。`
    : "";
  return `${lead}这本书的${parts.join("、")}。${tail}`;
}

/* 左栏（和窄屏下的书库对话框）：书库标题 + 导入、参考书活动、筛选、书单。 */
export function SrLibrary({ bookId, onSelect, onImport, onDelete, delBusy }) {
  useSrStore("books", "activity", "deep");
  const [query, setQuery] = React.useState("");
  const all = srBooks();
  const sorted = srSortBooks(all, srMeta().appliedBookIds);
  const showFilter = all.length > 8;
  const books = showFilter ? srFilterBooks(sorted, query) : sorted;
  const { phase, error, code } = srBooksState();
  return (
    <div className="sr-lib">
      <header className="sr-books-head">
        <h2 className="sr-books-title text-serif">参考书库</h2>
        {/* 书库一栏的「导入」是次要动作：实心主按钮留给右边这本书的下一步（空书库时由中间的「导入第一本参考书」担任） */}
        <button type="button" className="btn btn-ghost btn-sm" onClick={onImport}><I.Plus size={13} /> 导入参考书</button>
      </header>

      <SrActivityPanel onOpenBook={onSelect} />

      {showFilter && (
        <div className="sr-book-filter">
          <I.Search size={13} />
          <input className="input" type="search" placeholder="按书名或作者筛选" aria-label="筛选参考书" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      )}

      {phase === "loading" && !all.length ? (
        <ul className="sr-book-list" aria-busy="true" aria-label="正在读取参考书库">
          {[0, 1, 2].map((i) => <li key={i} className="sr-book-skel" aria-hidden="true"><span /><span /></li>)}
        </ul>
      ) : phase === "error" && !all.length ? (
        /* 读书库失败只在中间那一处讲原因、给「重试」；左栏只标一行，不再并排两份同样的报错 */
        <p className="sr-lib-empty sr-lib-error" role="status" title={[error, code && `错误代码：${code}`].filter(Boolean).join("\n")}>
          <I.AlertTriangle size={13} aria-hidden="true" /> 读不到书库
        </p>
      ) : !all.length ? (
        <p className="sr-lib-empty">还没有参考书。导入一本你想学习文风的书。</p>
      ) : (
        <ul className="sr-book-list">
          {books.map((b) => (
            <SrBookRow key={b.id} book={b} active={bookId === b.id} onSelect={onSelect} onDelete={onDelete} delBusy={delBusy} />
          ))}
          {showFilter && books.length === 0 && <li className="sr-lib-empty">没有书名或作者含「{query}」的参考书。</li>}
        </ul>
      )}
    </div>
  );
}

/* 书单一行：书脊色、书名、万字、流水线徽标；悬停 / 聚焦时露出删除按钮。 */
function SrBookRow({ book: b, active, onSelect, onDelete, delBusy }) {
  const p = srPipelineFor(b);
  const wan = (b.chars / 10000).toFixed(b.chars >= 100000 ? 0 : 1);
  return (
    <li className={`sr-book-item${delBusy === b.id ? " is-deleting" : ""}`}>
      <button
        type="button"
        className={`sr-book ${active ? "is-active" : ""}`}
        aria-current={active ? "true" : undefined}
        title={`${b.title}${b.author ? ` · ${b.author}` : ""} · ${b.chars.toLocaleString()} 字`}
        onClick={() => onSelect(b.id)}
      >
        <span className={`sr-book-spine spine-${b.color}`} aria-hidden="true" />
        <span className="sr-book-body">
          <span className="sr-book-title text-serif">{b.title}</span>
          <span className="sr-book-meta">{b.author ? `${b.author} · ` : ""}{wan} 万字</span>
        </span>
        {p && <Tag tone={p.tone} className="sr-book-chip">{p.label}</Tag>}
      </button>
      <button
        type="button"
        className={`sr-book-del${delBusy === b.id ? " is-busy" : ""}`}
        data-sr-del={b.id}
        title="删除这本参考书"
        aria-label={`删除参考书《${b.title}》`}
        disabled={!!delBusy}
        onClick={() => onDelete(b)}
      >
        {delBusy === b.id ? <Spinner size={12} /> : <I.Trash size={13} />}
      </button>
    </li>
  );
}

/* ==========================================================
   导入
   ========================================================== */
export const SR_CLOUD_POLICIES = [
  {
    id: "local_only",
    label: "仅保存在本机",
    badge: "默认 · 最私密",
    detail: "原文不发送给云端模型。可以导入与本地分段，但云端风格抽取会保持关闭。",
  },
  {
    id: "segments_only",
    label: "只发送所需段落",
    badge: "折中",
    detail: "抽取时仅发送任务需要的分段，不上传整本；适合希望使用云端分析又控制出域范围的情况。",
  },
  {
    id: "allow_full_cloud",
    label: "允许全文上云",
    badge: "能力完整",
    detail: "服务可按任务发送更大范围乃至全文给已配置的模型供应商；只应在你确认拥有授权时使用。",
  },
];

/* 导入权属声明（后端 ingest._normalize_rights_declaration §5.9）：
   - analysis_rights：作者确认有权对这本书做风格分析（所有策略都要勾）
   - send_rights：作者确认有权把段落送往云端模型；非 local_only 策略后端强制要求为 true，
     否则 400 STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED。声明缺失绝不由前端默认补上。 */
export const SR_RIGHTS_TERMS = {
  analysis: "我确认拥有对这本书进行风格分析的权利，且只用于学习抽象技法，不复刻原文、人物或桥段。",
  send: "我确认拥有这本书的发送权，并授权系统按所选策略把段落发送给已配置的云端模型供应商。",
};

function srPolicyNeedsSendRights(cloudPolicy) {
  return cloudPolicy !== "local_only";
}

/* 声明是否满足所选策略：分析权必勾；云端策略额外要求发送权。 */
export function srRightsReady(cloudPolicy, rights) {
  if (!rights || rights.analysis_rights !== true) return false;
  return !srPolicyNeedsSendRights(cloudPolicy) || rights.send_rights === true;
}

/* 把对话框收集的声明整理成后端 rights_declaration 的形状；未声明时返回 null（后端记 declared=false）。 */
function srBuildRightsDeclaration(cloudPolicy, rights) {
  if (!rights || rights.declared === false) return null;
  return {
    declared: true,
    analysis_rights: rights.analysis_rights === true,
    send_rights: srPolicyNeedsSendRights(cloudPolicy) && rights.send_rights === true,
    declared_by: getOperatorRef(),
  };
}

/* 与后端 ingest._REFERENCE_BOOK_SUFFIXES 一致：只接受纯文本 / Markdown。 */
const SR_IMPORT_ACCEPT = ".txt,.md,.markdown";
function srFileTitle(name) { return String(name || "").replace(/\.[^.]+$/, "").trim(); }

/* 导入参考书：导入单子交来 { file, title, author } 时直接 srRunImport（multipart，带幂等键 + 权属声明 +
   作者，进度在「参考书活动」）；没有第三个参数时退回旧流程（隐藏文件框 + 书名 prompt），给老调用方和测试。 */
export function srImportBook(cloudPolicy = "local_only", rights = null, picked = null) {
  if (!SR_CLOUD_POLICIES.some((item) => item.id === cloudPolicy)) {
    throw new Error("未知的参考书数据策略");
  }
  const rightsDeclaration = srBuildRightsDeclaration(cloudPolicy, rights);
  // 与后端同一条红线：云端策略没有 send_rights=true 就不打开文件选择器，更不会发请求。
  if (srPolicyNeedsSendRights(cloudPolicy) && !(rightsDeclaration && rightsDeclaration.send_rights)) {
    throw new Error("云端策略需要作者先确认发送权声明；未获授权请改用「仅保存在本机」。");
  }
  const run = async (file, title, authorLabel) => {
    try {
      await srRunImport({ file, title, authorLabel, cloudPolicy, rightsDeclaration });
    } catch (e) {
      // 失败一律提示：「参考书活动」里也记着原因，但 ≤1280 时它收在「参考书库」抽屉里、
      // 作者也可能已经切走——导入单子一关，页面上什么都没变，不提示就等于没发生。
      srNotify(`导入失败：${(e && e.message) || e}。原因也记在${SR_ACTIVITY_WHERE}里。`);
    }
  };
  if (picked && picked.file) {
    run(picked.file, String(picked.title || "").trim() || srFileTitle(picked.file.name), picked.author || null);
    return;
  }
  const input = document.createElement("input");
  input.type = "file";
  input.accept = SR_IMPORT_ACCEPT;
  input.onchange = async () => {
    const f = input.files && input.files[0];
    if (!f) return;
    const title = (window.prompt("书名（用于书库显示）", srFileTitle(f.name)) || "").trim();
    if (!title) return;
    await run(f, title, null);
  };
  input.click();
}

/* 导入单子：原文能不能离开本机 + 权属声明、选文件、书名与作者。
   提交时把文件、书名、作者一起交给 onChoose(policy, rights, { file, title, author })。 */
export function SrImportDialog({ open, onClose, onChoose }) {
  const [policy, setPolicy] = React.useState("local_only");
  const [analysisRights, setAnalysisRights] = React.useState(false);
  const [sendRights, setSendRights] = React.useState(false);
  const [file, setFile] = React.useState(null);
  const [title, setTitle] = React.useState("");
  /* 作者亲手改过书名（哪怕是在选文件之前）：之后换文件也不再用文件名覆盖 */
  const [titleTouched, setTitleTouched] = React.useState(false);
  const [author, setAuthor] = React.useState("");
  const [fileError, setFileError] = React.useState(null);
  const [dragOver, setDragOver] = React.useState(false);
  const titleId = React.useId();
  const descId = React.useId();
  const fileInputId = React.useId();
  const needsSend = srPolicyNeedsSendRights(policy);
  // 发送权只在云端策略下有意义；切回 local_only 时声明里恒为 false，不带走多余授权。
  const declaration = { declared: true, analysis_rights: analysisRights, send_rights: needsSend && sendRights };
  const rightsReady = srRightsReady(policy, declaration);
  const ready = rightsReady && !!file && !!title.trim();

  React.useEffect(() => {
    if (!open) return;
    setPolicy("local_only");
    setAnalysisRights(false);
    setSendRights(false);
    setFile(null);
    setTitle("");
    setTitleTouched(false);
    setAuthor("");
    setFileError(null);
    setDragOver(false);
  }, [open]);

  const takeFile = (f) => {
    if (!f) return;
    if (!/\.(txt|md|markdown)$/i.test(f.name || "")) {
      setFileError("只能导入纯文本（.txt）或 Markdown（.md）文件。");
      return;
    }
    setFileError(null);
    setFile(f);
    // 书名默认取文件名；作者亲手填过（且没清空）就不再覆盖
    if (!titleTouched || !title.trim()) setTitle(srFileTitle(f.name));
  };

  const submit = () => {
    if (!ready) return;
    onChoose(policy, declaration, { file, title: title.trim(), author: author.trim() });
  };

  return (
    <WsDialog
      open={open}
      onClose={onClose}
      labelledBy={titleId}
      describedBy={descId}
      size="lg"
      className="sr-import-dialog"
      portal={false}
    >
      <header className="ws-dialog-head">
        <div>
          <h2 id={titleId} className="ws-dialog-title text-serif">导入参考书</h2>
          <p id={descId} className="ws-dialog-desc">
            先定这本书能不能离开本机，再选文件。选择会随书保存并约束之后的抽取；系统只学抽象技法，不复刻人物、桥段或原句。
          </p>
        </div>
        <button type="button" className="ws-dialog-x" aria-label="关闭导入" onClick={onClose}><I.X size={16} /></button>
      </header>
      <div className="ws-dialog-body sr-import-body">
        <fieldset className="sr-policy-list">
          <legend className="sr-policy-legend">原文数据使用范围</legend>
          {SR_CLOUD_POLICIES.map((item) => (
            <label key={item.id} className={`sr-policy ${policy === item.id ? "is-selected" : ""}`}>
              <input type="radio" name="sr-cloud-policy" value={item.id} checked={policy === item.id} onChange={() => setPolicy(item.id)} />
              <span className="sr-policy-mark" aria-hidden="true" />
              <span className="sr-policy-copy">
                <span className="sr-policy-title">{item.label}<em>{item.badge}</em></span>
                <span className="sr-policy-detail">{item.detail}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <p className="sr-import-notice"><I.ShieldCheck size={14} /><span>「仅保存在本机」不会悄悄改成上云；以后需要云端抽取，要用更开放的策略重新导入。</span></p>
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
          {!rightsReady && (
            <p className="sr-rights-hint" data-testid="sr-rights-hint">
              {needsSend ? "云端策略需要同时确认分析权与发送权，后端不接受未声明的上云导入；未获授权请改选「仅保存在本机」。" : "请先确认分析权，再导入。"}
            </p>
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
            <input className="input" data-testid="sr-import-author" value={author} maxLength={120} placeholder="书库和画像标题里显示" onChange={(e) => setAuthor(e.target.value)} />
          </label>
        </div>
      </div>
      <footer className="ws-dialog-foot sr-import-foot">
        {!ready && <span className="sr-import-why">{!rightsReady ? "先确认权属声明" : !file ? "还没选文件" : "还没填书名"}</span>}
        <button type="button" className="btn btn-ghost" onClick={onClose}>取消</button>
        <button
          type="button"
          className="btn btn-accent"
          data-testid="sr-import-choose-file"
          disabled={!ready}
          onClick={submit}
        ><I.FileInput size={14} /> 导入</button>
      </footer>
    </WsDialog>
  );
}
