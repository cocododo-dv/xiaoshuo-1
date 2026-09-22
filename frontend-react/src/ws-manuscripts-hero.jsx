import React from "react";
import { I } from "./icons.jsx";
import { wsToast } from "./ws-notify.jsx";
import { CloseButton, PageHeader, Segmented, Spinner, Tag } from "./ws-ui.jsx";
import { CHAPTER_STATE_META, CHAPTER_STATE_ORDER, chapterLabel, chapterStateMeta } from "./ws-labels.js";
import { manuCompile, manuScopeProblem } from "./ws-manuscripts-compile.js";
import { manuDownload, manuRefreshChapters, manuSnapshotOf } from "./ws-manuscripts-workflow.js";

/* ==========================================================
   成稿中心页头：页名（和其他页一样的 PageHeader）、作品名与一句进度、一条按章分段的进度条、统一导出。
   进度条只是看的（role=img），选章只在左栏——以前书脊格子和左栏是同一份数据的两个选择器，
   1100 宽时还把正文挤得只剩三百来像素。
   ========================================================== */

const { useEffect, useRef, useState } = React;

function ManuHero({ book, cells, activeId, stats, exportCtx }) {
  const counts = {};
  cells.forEach((c) => { counts[c.stage] = (counts[c.stage] || 0) + 1; });
  const legend = CHAPTER_STATE_ORDER.filter((stage) => counts[stage]).map((stage) => ({ stage, n: counts[stage], meta: CHAPTER_STATE_META[stage] }));
  const summary = `整书 ${cells.length} 章：${legend.map((l) => `${l.meta.label} ${l.n}`).join("，")}`;
  const goal = Number(book.goalWords) || 0;
  /* 页头和其他页同一个语法（PageHeader）：标题是「成稿中心」，作品名和进度是 meta；
     按章分段的进度条和统一导出排在动作位（一行排开，窄了再折行） */
  return (
    <PageHeader
      className="ms-hero"
      testId="manuscripts-head"
      title="成稿中心"
      meta={(
        <>
          <span className="ms-hero-work" title={book.title}>{book.title}</span>
          <span>已定稿 <b>{stats.approvedCount}</b>/{cells.length} 章</span>
          <span>定稿字数 <b>{stats.approvedWords.toLocaleString()}</b>{goal ? ` / ${goal.toLocaleString()}` : ""}</span>
          {stats.reviewCount > 0 && <Tag tone="warn" className="ms-hero-flag">{stats.reviewCount} 章等你批准</Tag>}
        </>
      )}
      actions={(
        <>
          <div className="ms-progress-wrap">
            <div className="ms-progress" role="img" aria-label={summary}>
              {cells.map((c) => (
                <span
                  key={c.id}
                  className={`ms-progress-cell ${c.plan ? "is-plan" : ""} ${activeId === c.id ? "is-picked" : ""}`}
                  data-stage={c.stage}
                  title={`${chapterLabel(c, { maxTitle: Infinity })}：${chapterStateMeta(c.stage).label}`}
                />
              ))}
            </div>
            <div className="ms-progress-legend" aria-hidden="true">
              {legend.map((l) => <span key={l.stage} className="ms-leg"><span className="ms-leg-dot" data-stage={l.stage} />{l.meta.label} {l.n}</span>)}
            </div>
          </div>
          <ManuExport ctx={exportCtx} />
        </>
      )}
    />
  );
}

const EXPORT_FORMATS = [["md", "Markdown"], ["txt", "纯文本"], ["doc", "Word"]];

/* 统一导出：逐章核验服务端权威正文后编译下载。 */
function ManuExport({ ctx }) {
  const { catChs = [], book = {}, chs = [], pickedId } = ctx || {};
  const approvedIds = chs.filter((c) => c.stage === "approved").map((c) => c.id);
  const [open, setOpen] = useState(false);
  const [fmt, setFmt] = useState("md");
  /* 默认范围：有定稿就是「已定稿」，否则「当前章」——以前默认「仅已批准 · 0」，一打开就是报错 */
  const [scope, setScope] = useState(() => (approvedIds.length ? "approved" : "current"));
  const [toc, setToc] = useState(true);
  const [appendix, setAppendix] = useState(false);
  const [exportState, setExportState] = useState({ busy: false, error: "", note: "" });
  const ref = useRef(null);
  const btnRef = useRef(null);
  const exportBusyRef = useRef(false);
  const closeTimerRef = useRef(null);

  useEffect(() => () => window.clearTimeout(closeTimerRef.current), []);

  const close = (restoreFocus) => {
    setOpen(false);
    if (restoreFocus && btnRef.current) btnRef.current.focus();
  };
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); close(true); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 打开时焦点进面板本身（role=dialog，读屏报出「统一导出」，下一下 Tab 就是面板里的第一个控件）。
     过去焦点留在按钮上，Tab 要穿过整块面板才知道它开着。不直接落在某个单选上：一打开就开始核验，
     核验中所有控件都是禁用的，落在它们上面的焦点会被浏览器丢回 <body>。
     焦点离开这一块（Tab 出去、点到页面别处的控件）就收起——非模态浮层不把焦点关住，也不在身后留一块开着的面板；
     焦点只是掉到 <body>（点了面板空白、控件在核验中变成禁用）不算离开。 */
  const popRef = useRef(null);
  useEffect(() => {
    if (open && popRef.current) popRef.current.focus({ preventScroll: true });
  }, [open]);
  const onBlurWithin = (e) => {
    const next = e.relatedTarget;
    if (!open || !next || !ref.current || ref.current.contains(next)) return;
    setOpen(false);
  };

  // 打开时按当时有没有定稿重选默认范围（作者上次选的范围若已经空了，不停在一个空范围上）
  useEffect(() => {
    if (open && scope === "approved" && !approvedIds.length) setScope("current");
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const scopeIds =
    scope === "approved" ? approvedIds :
    scope === "current" ? (pickedId ? [pickedId] : []) :
    chs.map((c) => c.id);
  const scopeWords = chs.filter((c) => scopeIds.includes(c.id)).reduce((sum, c) => sum + (c.words || 0), 0);
  const scopeKey = scopeIds.join("|");
  const scopeProblem = manuScopeProblem(catChs, scopeIds, manuSnapshotOf);
  const canExport = scopeIds.length > 0 && !scopeProblem && !exportState.busy;

  /* 面板打开 / 范围切换时补拉目标章（30 秒内拉过的直接复用），生成按钮只在服务端逐章确认后放行。 */
  useEffect(() => {
    if (!open) return undefined;
    let live = true;
    setExportState({ busy: true, error: "", note: "" });
    manuRefreshChapters(catChs, scopeIds, { force: false }).then(() => {
      if (!live) return;
      const problem = manuScopeProblem(catChs, scopeIds, manuSnapshotOf);
      setExportState({ busy: false, error: problem, note: problem ? "" : "服务端正文已核验。" });
    }).catch((e) => {
      if (live) setExportState({ busy: false, error: (e && e.message) || "导出前核验失败。", note: "" });
    });
    return () => { live = false; };
  }, [open, scopeKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async () => {
    if (!canExport || exportBusyRef.current) return;
    exportBusyRef.current = true;
    setExportState({ busy: true, error: "", note: "" });
    try {
      await manuRefreshChapters(catChs, scopeIds, { force: true });
      const problem = manuScopeProblem(catChs, scopeIds, manuSnapshotOf);
      if (problem) throw new Error(problem);
      const out = manuCompile(book, catChs, scopeIds, fmt, { toc, appendix, snapshotOf: manuSnapshotOf });
      if (!manuDownload(out.name, out.content, out.mime)) throw new Error("浏览器未能生成下载文件。");
      setExportState({ busy: false, error: "", note: `已生成「${out.name}」` });
      wsToast({ message: `已下载「${out.name}」`, tone: "ok" });
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = window.setTimeout(() => {
        setExportState({ busy: false, error: "", note: "" });
        setOpen(false);
      }, 1400);
    } catch (e) {
      setExportState({ busy: false, error: (e && e.message) || "导出失败。", note: "" });
    } finally {
      exportBusyRef.current = false;
    }
  };

  // 一行状态：核验中 / 问题（只说一次）/ 已核验 + 范围大小
  const statusLine = exportState.busy
    ? "正在核验服务端正文…"
    : exportState.error || scopeProblem || `约 ${scopeWords.toLocaleString()} 字 · ${scopeIds.length} 章`;
  const statusIsError = !exportState.busy && !!(exportState.error || scopeProblem);

  return (
    <div className="ms-export" ref={ref} onBlur={onBlurWithin}>
      {/* 整书导出是次要动作：这一页唯一的实心主按钮留给阅读器页脚里这一章的下一步（续写 / 核对正史 / 送审） */}
      <button ref={btnRef} type="button" className={`btn btn-ghost ms-export-btn ${open ? "is-open" : ""}`} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((o) => !o)}>
        <I.UploadCloud size={15} /> 统一导出
      </button>
      {open && (
        <div className="ms-export-pop" ref={popRef} tabIndex={-1} role="dialog" aria-label="统一导出">
          <div className="ms-export-hd">
            <span>统一导出</span>
            <CloseButton onClick={() => close(true)} size="xs" />
          </div>

          <div className="ms-export-field">
            <div className="ms-export-lab">范围</div>
            <Segmented block label="导出范围" value={scope} onChange={setScope} options={[
              { value: "approved", label: "已定稿", count: approvedIds.length, disabled: exportState.busy || !approvedIds.length },
              { value: "current", label: "当前章", disabled: exportState.busy || !pickedId },
              { value: "all", label: "全书", count: chs.length, disabled: exportState.busy || !chs.length },
            ]} />
          </div>

          <div className="ms-export-field">
            <div className="ms-export-lab">格式</div>
            <Segmented block label="导出格式" value={fmt} onChange={setFmt}
              options={EXPORT_FORMATS.map(([value, label]) => ({ value, label, disabled: exportState.busy }))} />
          </div>

          <div className="ms-export-field">
            <div className="ms-export-lab">选项</div>
            <label className="ms-export-opt"><input type="checkbox" checked={toc} disabled={exportState.busy} onChange={(e) => setToc(e.target.checked)} /> 生成目录</label>
            <label className="ms-export-opt"><input type="checkbox" checked={appendix} disabled={exportState.busy} onChange={(e) => setAppendix(e.target.checked)} /> 附戏剧卡附录</label>
          </div>

          <div className="ms-export-foot">
            <span className={`ms-export-status ${statusIsError ? "is-error" : ""}`} role={statusIsError ? "alert" : "status"}>
              {exportState.note && !exportState.busy && !statusIsError ? exportState.note : statusLine}
            </span>
            <button type="button" className="btn btn-accent btn-sm" data-testid="manuscript-export-run" disabled={!canExport} onClick={run}>
              {exportState.busy ? <Spinner size={13} /> : <I.Download size={13} />} {exportState.busy ? "核验中…" : "生成并下载"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export { ManuHero };
