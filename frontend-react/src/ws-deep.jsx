import React from "react";
import { I } from "./icons.jsx";
import { wsKey } from "./ws-works.jsx";
import { apiGet, apiPatch, apiPost } from "./lib/client.js";
import { CloseButton, IconButton, Notice, Spinner, Tag } from "./ws-ui.jsx";
import { wrAiError } from "./ws-writer-ai.js";
import { wrRangeForOffsets, wrRangeForText } from "./ws-writer-manuscript.js";
import { useWrInert } from "./ws-writer-hooks.js";
import { qSevLabel, qSevTone } from "./ws-quality-model.js";

/* ==========================================================
   ws-deep — 写作台深改面板（2026-09-22 场景诊断统一）
   ----------------------------------------------------------
   诊断只有一份，在服务端：GET /api/v1/scenes/{id}/deep-review 把 21 维规则体检、段落节奏
   （贴邻叠句 / 段落偏长 / 句首重复——原先是这里三条本地正则）、起草台的准定稿评审和 AI 深评
   合成同一种发现形状（signal_id / source / dimension / severity / issue / recommendation /
   evidence{paragraph_index,start,end,excerpt}），文学质量视图读的也是这份，所以一条发现从那边
   点过来，这里就是同一条。
   · wrDxFetch / wrDxRunAi     取诊断 / 跑「AI 深评」（POST 同一路径；无模型时后端 409，这里给「去系统设置」）
   · wrDeepMark / wrDeepUnmark 按发现的段落序号 + 证据文字在编辑器里标高亮
   · WrDeepDrawer              右栏面板：来源筛选、AI 深评块、发现列表（展开一条看证据 / 改法 / 为什么）、
                               已忽略、最近的决定
   深改只诊断、不改字：每一条给「选中这一句去改写」/「按诊断改写」——回到起草姿态、选中那一句，
   改写走选区工具条那条真实的改写接口，并把这条发现（id / 维度 / 改法）一起发给后端。
   忽略按 signal_id 记在服务端（deep-review/preferences，带修订号），文学质量视图和成稿门都认。
   姿态本身（进出、偏好同步、选中去改写）在 ws-writer-deep-posture.js。
   ========================================================== */

const dxKey = (base) => (wsKey ? wsKey(base) : base);

/* ---- 决定日志 / 忽略清单（按场景写穿到本机，服务端是真相）---- */
function wrDxLog(sid) {
  try { return JSON.parse(localStorage.getItem(dxKey("wr-deep-log:" + sid))) || []; } catch (e) { return []; }
}
function wrDxPushLog(sid, text) {
  const list = [{ at: Date.now(), text }, ...wrDxLog(sid)].slice(0, 30);
  try { localStorage.setItem(dxKey("wr-deep-log:" + sid), JSON.stringify(list)); } catch (e) {}
  return list;
}
function wrDxSkips(sid) {
  try { return new Set(JSON.parse(localStorage.getItem(dxKey("wr-deep-skip:" + sid))) || []); } catch (e) { return new Set(); }
}
function wrDxWriteSkips(sid, set) {
  try { localStorage.setItem(dxKey("wr-deep-skip:" + sid), JSON.stringify([...set])); } catch (e) {}
}
function wrDxAddSkip(sid, key) {
  const s = wrDxSkips(sid); s.add(key); wrDxWriteSkips(sid, s);
}
function wrDxRemoveSkip(sid, key) {
  const s = wrDxSkips(sid); s.delete(key); wrDxWriteSkips(sid, s);
}
function wrDxClearSkips(sid) {
  try { localStorage.removeItem(dxKey("wr-deep-skip:" + sid)); } catch (e) {}
}

function wrDxSnapshot(sid) {
  return { decision_log: wrDxLog(sid), ignored_issue_keys: [...wrDxSkips(sid)] };
}

function wrDxApplyPreferences(sid, preferences) {
  const decisionLog = Array.isArray(preferences?.decision_log) ? preferences.decision_log.slice(0, 30) : [];
  const ignoredKeys = Array.isArray(preferences?.ignored_issue_keys)
    ? [...new Set(preferences.ignored_issue_keys)].slice(0, 200)
    : [];
  try { localStorage.setItem(dxKey("wr-deep-log:" + sid), JSON.stringify(decisionLog)); } catch (e) {}
  try { localStorage.setItem(dxKey("wr-deep-skip:" + sid), JSON.stringify(ignoredKeys)); } catch (e) {}
  return { decision_log: decisionLog, ignored_issue_keys: ignoredKeys };
}

function wrDxMergePreferences(remote, local, { localIgnoredAuthoritative = false } = {}) {
  const seenLogs = new Set();
  const decisionLog = [...(local?.decision_log || []), ...(remote?.decision_log || [])]
    .filter((entry) => {
      const key = `${entry?.at ?? ""}:${entry?.text ?? ""}`;
      if (!entry?.text || seenLogs.has(key)) return false;
      seenLogs.add(key);
      return true;
    })
    .sort((a, b) => Number(b.at || 0) - Number(a.at || 0))
    .slice(0, 30);
  const ignoredSource = localIgnoredAuthoritative
    ? (local?.ignored_issue_keys || [])
    : [...(local?.ignored_issue_keys || []), ...(remote?.ignored_issue_keys || [])];
  return {
    decision_log: decisionLog,
    ignored_issue_keys: [...new Set(ignoredSource)].slice(0, 200),
  };
}

async function wrDxLoadPreferences(sid) {
  return apiGet(`/api/v1/scenes/${encodeURIComponent(sid)}/deep-review/preferences`);
}

async function wrDxSavePreferences(sid, snapshot, baseRevisionNo) {
  return apiPatch(`/api/v1/scenes/${encodeURIComponent(sid)}/deep-review/preferences`, {
    decision_log: (snapshot?.decision_log || []).slice(0, 30),
    ignored_issue_keys: [...new Set(snapshot?.ignored_issue_keys || [])].slice(0, 200),
    base_revision_no: baseRevisionNo,
  });
}

/* ---- 诊断：服务端一份 ---- */
async function wrDxFetch(backendId) {
  return apiGet(`/api/v1/scenes/${encodeURIComponent(backendId)}/deep-review`);
}
async function wrDxRunAi(backendId) {
  return apiPost(`/api/v1/scenes/${encodeURIComponent(backendId)}/deep-review`, {});
}
/* 「AI 看这一处」：body 是 { signal_id } （复核一条发现）或 { paragraph_index, excerpt }（独立看一段），可带 question */
async function wrDxReviewPassage(backendId, body) {
  return apiPost(`/api/v1/scenes/${encodeURIComponent(backendId)}/deep-review/passage`, body || {});
}

/* 发现的 ignored 按本机忽略清单重算（忽略 / 恢复不必等服务端往返） */
function wrDxWithIgnored(findings, skips) {
  return (findings || []).map((f) => ({ ...f, ignored: skips.has(f.signal_id) }));
}

const WR_DX_SOURCES = {
  rules: "规则",
  craft: "节奏",
  review: "起草评审",
  ai: "AI 深评",
};
const WR_DX_SOURCE_ORDER = ["rules", "craft", "review", "ai"];
const WR_DX_LENS = { story: "故事", character: "人物", prose: "文字", reader: "读者", theme: "主题" };
const WR_DX_ORIGIN = { passage: "局部", chapter: "通读" };
const WR_DX_VERDICT = {
  holds: { label: "AI：成立", tone: "accent" },
  partly: { label: "AI：部分成立", tone: "warn" },
  does_not_hold: { label: "AI：不成立", tone: "ok" },
  no_finding: { label: "AI：没有要改的", tone: "ok" },
};
/* 类名写全，设计守卫按字面找引用 */
const WR_DX_SEV_CLASS = { blocking: "sev-blocking", revision: "sev-revision", taste: "sev-taste", info: "sev-info" };
const sevClass = (sev) => WR_DX_SEV_CLASS[sev] || WR_DX_SEV_CLASS.info;

/* ---- 编辑器内标注 ---- */
function wrDeepUnmark(el) {
  if (!el) return;
  el.querySelectorAll("mark.wr-dx").forEach((mk) => {
    const parent = mk.parentNode;
    while (mk.firstChild) parent.insertBefore(mk.firstChild, mk);
    parent.removeChild(mk);
    parent.normalize();
  });
  el.querySelectorAll(".wr-dx-para").forEach((p) => {
    p.classList.remove("wr-dx-para", "is-active");
    Object.values(WR_DX_SEV_CLASS).forEach((name) => p.classList.remove(name));
    p.removeAttribute("data-dx");
  });
}

/* 一条发现在编辑器里的 Range：先按段落序号 + 偏移，再按证据文字在那一段里找，最后全文找
   （作者在前面加了段、改了几个字，标注仍落在那句话上）。返回 { block, range }；找不到给 null。 */
function wrDxRangeFor(el, finding) {
  const ev = finding && finding.evidence;
  if (!el || !ev) return null;
  const blocks = Array.from(el.querySelectorAll("p, blockquote"));
  let block = Number.isInteger(ev.paragraph_index) ? blocks[ev.paragraph_index] : null;
  let range = null;
  if (block) {
    if (Number.isFinite(ev.start) && Number.isFinite(ev.end) && ev.end > ev.start) {
      range = wrRangeForOffsets(block, ev.start, ev.end);
      if (range && ev.excerpt && range.toString() !== ev.excerpt) range = null;
    }
    if (!range && ev.excerpt) range = wrRangeForText(block, ev.excerpt);
  }
  if (!range && ev.excerpt) {
    for (const candidate of blocks) {
      const hit = wrRangeForText(candidate, ev.excerpt);
      if (hit) { block = candidate; range = hit; break; }
    }
  }
  return range ? { block, range } : null;
}

function wrDeepMark(el, findings, activeKey) {
  if (!el) return;
  wrDeepUnmark(el);
  /* 选中的跨段发现：另一段的那句也标出来（同一条 data-dx，淡一层） */
  const activeFinding = (findings || []).find((f) => f.signal_id === activeKey && !f.ignored);
  if (activeFinding && activeFinding.related && activeFinding.related.excerpt && !activeFinding.related.stale) {
    const hit = wrDxRangeFor(el, { evidence: activeFinding.related });
    if (hit && hit.range.toString().trim() !== (hit.block.textContent || "").trim()) {
      try {
        const mk = document.createElement("mark");
        mk.className = "wr-dx is-related";
        mk.setAttribute("data-dx", activeFinding.signal_id);
        mk.appendChild(hit.range.extractContents());
        hit.range.insertNode(mk);
      } catch (e) { /* 标不上就只标焦点 */ }
    }
  }
  (findings || []).forEach((f) => {
    if (f.ignored || !f.evidence) return;
    const hit = wrDxRangeFor(el, f);
    if (!hit) return;
    const { block, range } = hit;
    const active = f.signal_id === activeKey;
    const markParagraph = () => {
      block.classList.add("wr-dx-para", sevClass(f.severity));
      if (active) block.classList.add("is-active");
      block.setAttribute("data-dx", f.signal_id);
    };
    const whole = range.toString().trim() === (block.textContent || "").trim();
    if (whole) { markParagraph(); return; }
    try {
      const mk = document.createElement("mark");
      mk.className = `wr-dx ${sevClass(f.severity)}${active ? " is-active" : ""}`;
      mk.setAttribute("data-dx", f.signal_id);
      /* extract + insert 比 surroundContents 稳：证据跨过实体高亮的 span 也能包起来 */
      mk.appendChild(range.extractContents());
      range.insertNode(mk);
    } catch (e) {
      markParagraph();
    }
  });
}

/* ==========================================================
   WrDeepDrawer — 写作台右栏 · 深改面板
   ========================================================== */
/* 深评失败：无模型（后端 409 + author_action）给「去系统设置」，其余给重试（与写作台其他 AI 入口同一套翻译） */
function DxAiError({ error, onRetry, onOpenSettings }) {
  const info = wrAiError(error);
  const configOnly = info.kind === "config";
  const retry = !configOnly && onRetry
    ? <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>{info.actionLabel}</button> : null;
  const settings = info.offersSettings && onOpenSettings
    ? <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenSettings}>去系统设置</button> : null;
  return (
    <Notice tone={configOnly ? "warn" : "danger"} className="wr-dxd-notice" actions={retry || settings ? <>{retry}{settings}</> : null}>
      {info.message}
    </Notice>
  );
}

function DxAiBlock({ ai, busy, error, onRun, onRetry, onOpenSettings }) {
  const status = (ai && ai.status) || "not_run";
  const score = ai && ai.overall_score != null ? Math.round(ai.overall_score * 100) : null;
  const when = ai && ai.created_at ? new Date(ai.created_at) : null;
  const whenText = when && !Number.isNaN(when.getTime())
    ? when.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "";
  const brief = (ai && ai.revision_brief) || [];
  return (
    <section className="wr-dxd-ai" aria-label="AI 深评">
      <div className="wr-dxd-ai-head">
        <span className="wr-dxd-sub"><I.Sparkles size={13} /> AI 深评</span>
        <button type="button" className="btn btn-accent btn-sm" disabled={busy} onClick={onRun}>
          {busy ? <Spinner size={13} /> : <I.Sparkles size={13} />} {busy ? "深评中…" : (status === "not_run" ? "AI 深评" : "重新深评")}
        </button>
      </div>
      <p className="wr-dxd-ai-meta">
        {status === "not_run" && "还没跑过：让模型按十个文学维度和五个镜头读一遍本场，发现会并入下面的清单。"}
        {status === "current" && `${whenText}${score != null ? ` · 总分 ${score}` : ""}，对着现在这份正文。`}
        {status === "stale" && `${whenText ? whenText + " 的深评是改前的" : "这份深评是改前的"}——正文已经改过，证据找不到的会标出来；要按现在的稿子判断就重新深评。`}
      </p>
      {error && <DxAiError error={error} onRetry={onRetry} onOpenSettings={onOpenSettings} />}
      {brief.length > 0 && status !== "not_run" && (
        <ul className="wr-dxd-brief">
          {brief.slice(0, 4).map((line, i) => (
            <li key={i}>{line.action || line.recommendation || line.text || ""}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* 跨段发现（局部深评对照全场看出的矛盾 / 重复 / 承接）：另一段的段号与关系 */
function relatedTag(finding) {
  const related = finding && finding.related;
  if (!related) return "";
  const where = Number.isInteger(related.paragraph_index) ? `与第 ${related.paragraph_index + 1} 段` : "与另一段";
  return `${where}${related.label || "矛盾"}`;
}

function DxFindingRow({ finding, active, onPick }) {
  const src = WR_DX_SOURCES[finding.source] || finding.source;
  const carried = !!(finding.origin && finding.origin.carried_from);
  const calibrated = !!(finding.calibrated && finding.calibrated.kind);
  return (
    <button type="button" className={`wr-dxd-row ${active ? "is-active" : ""}`} aria-pressed={active} onClick={() => onPick(finding.signal_id)}>
      <span className={`wr-dxd-mark ${sevClass(finding.severity)}`} title={`严重程度：${qSevLabel(finding.severity)}`}>{finding.label || finding.dimension}</span>
      <span className="wr-dxd-body">
        <span className="wr-dxd-t">{finding.issue}</span>
        <span className="wr-dxd-h">
          <span className="wr-dxd-src">{src}{finding.lens && WR_DX_LENS[finding.lens] ? ` · ${WR_DX_LENS[finding.lens]}` : ""}{finding.origin && WR_DX_ORIGIN[finding.origin.kind] ? ` · ${WR_DX_ORIGIN[finding.origin.kind]}` : ""}{carried ? "（沿用上次）" : ""}</span>
          {finding.related && <span className="wr-dxd-related-tag">{relatedTag(finding)}</span>}
          {calibrated && <span className="wr-dxd-calibrated">参考作者也常这样</span>}
          {finding.opinion && WR_DX_VERDICT[finding.opinion.verdict] && <span className="wr-dxd-opinion-tag">{WR_DX_VERDICT[finding.opinion.verdict].label}</span>}
          {finding.stale && <span className="wr-dxd-stale">证据已不在正文里</span>}
          {!finding.evidence && !finding.stale && <span className="wr-dxd-stale">整场</span>}
        </span>
      </span>
    </button>
  );
}

/* 「AI 看这一处」对这条发现的判断：成立 / 部分成立 / 不成立 + 评语 + 这一处的改法 */
function DxOpinion({ finding, opinion, onRewrite, onIgnore }) {
  const meta = WR_DX_VERDICT[opinion.verdict] || WR_DX_VERDICT.no_finding;
  const canRewrite = !!(opinion.rewrite_brief && finding.evidence && finding.evidence.excerpt && onRewrite);
  return (
    <div className="wr-dxd-opinion">
      <div className="wr-dxd-opinion-head">
        <Tag tone={meta.tone}>{meta.label}</Tag>
        {opinion.status === "stale" && <span className="wr-dxd-stale">改前的判断</span>}
      </div>
      {opinion.assessment && <p className="wr-dxd-opinion-t">{opinion.assessment}</p>}
      {opinion.rewrite_brief && <p className="wr-dxd-fix">AI 的改法：{opinion.rewrite_brief}</p>}
      <div className="wr-dxd-row-acts">
        {canRewrite && (
          <button type="button" className="btn btn-accent btn-sm" onClick={() => onRewrite(finding, { instruction: opinion.rewrite_brief })}>
            <I.Sparkles size={13} /> 按 AI 的改法改写
          </button>
        )}
        {opinion.verdict === "does_not_hold" && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => onIgnore(finding)}>按 AI 的判断忽略</button>
        )}
      </div>
    </div>
  );
}

function DxFindingDetail({ finding, onSelect, onRewrite, onIgnore, onPassageReview, passageBusy, passageError, onOpenSettings, onLocateParagraph }) {
  const ev = finding.evidence;
  const canSelect = !!(ev && ev.excerpt && onSelect);
  /* 服务端只钉到段（偏移为空）、或整段就是证据（段落偏长）：选中的是一段 */
  const paragraphLevel = !!(ev && (ev.start == null || ev.end == null || finding.dimension === "long_paragraph"));
  const reviewing = passageBusy === finding.signal_id;
  const canReview = !!(ev && onPassageReview && !finding.stale);
  const reviewError = passageError && passageError.key === finding.signal_id ? passageError.error : null;
  const related = finding.related || null;
  const relatedIndex = related && Number.isInteger(related.paragraph_index) ? related.paragraph_index : null;
  return (
    <div className="wr-dxd-detail">
      {(ev && ev.excerpt) ? <blockquote className="wr-dxd-ev">{ev.excerpt}</blockquote>
        : (finding.context ? <blockquote className="wr-dxd-ev">{finding.context}</blockquote> : null)}
      {related && (
        <div className="wr-dxd-related">
          <span className="wr-dxd-sub">{relatedTag(finding)}{related.stale ? "（那句已不在正文里）" : ""}</span>
          <blockquote className="wr-dxd-ev">{related.excerpt}</blockquote>
          {relatedIndex != null && onLocateParagraph && (
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => onLocateParagraph(relatedIndex)}>看第 {relatedIndex + 1} 段</button>
          )}
        </div>
      )}
      {finding.recommendation && <p className="wr-dxd-fix">改法：{finding.recommendation}</p>}
      {finding.why && <p className="wr-dxd-why">{finding.why}</p>}
      {finding.opinion && <DxOpinion finding={finding} opinion={finding.opinion} onRewrite={onRewrite} onIgnore={onIgnore} />}
      {reviewError && <DxAiError error={reviewError} onRetry={() => onPassageReview(finding)} onOpenSettings={onOpenSettings} />}
      <div className="wr-dxd-row-acts">
        {canReview && (
          <button type="button" className="btn btn-ghost btn-sm" disabled={!!passageBusy} onClick={() => onPassageReview(finding)}
            title="让模型只看这一段：这条发现成不成立、这一处怎么改">
            {reviewing ? <Spinner size={13} /> : <I.Sparkles size={13} />} {reviewing ? "AI 在看…" : "AI 看这一处"}
          </button>
        )}
        {canSelect && finding.recommendation && !finding.stale && onRewrite && (
          <button type="button" className="btn btn-accent btn-sm" onClick={() => onRewrite(finding)}>
            <I.Sparkles size={13} /> 按诊断改写
          </button>
        )}
        {canSelect && !finding.stale && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => onSelect(finding)}>
            <I.Pen size={13} /> {paragraphLevel ? "选中这一段去改写" : "选中这一句去改写"}
          </button>
        )}
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => onIgnore(finding)}>忽略这一项</button>
      </div>
    </div>
  );
}

/* 独立看一段 / 一段范围（没有复核某条发现）的结果：判定 + 评语 + 这一处的改法；新发现已并进清单。
   模型看的是整场（焦点段标出），所以「看了第 2–3 段」也可能指出与别处的矛盾。 */
function DxPassageNote({ passage, onRewriteParagraph }) {
  if (!passage || passage.about_signal_id) return null;
  const meta = WR_DX_VERDICT[passage.verdict] || WR_DX_VERDICT.no_finding;
  const focus = Array.isArray(passage.focus_paragraphs) && passage.focus_paragraphs.length
    ? passage.focus_paragraphs
    : (Number.isInteger(passage.paragraph_index) ? [passage.paragraph_index] : []);
  const pid = focus.length === 1 ? focus[0] : null;
  const where = focus.length > 1 ? `第 ${focus[0] + 1}–${focus[focus.length - 1] + 1} 段` : (pid != null ? `第 ${pid + 1} 段` : "这一段");
  return (
    <section className="wr-dxd-passage" aria-label="AI 看这一段">
      <div className="wr-dxd-opinion-head">
        <span className="wr-dxd-sub"><I.Sparkles size={13} /> AI 看了{where}</span>
        <Tag tone={meta.tone}>{meta.label}</Tag>
        {passage.status === "stale" && <span className="wr-dxd-stale">改前的判断</span>}
      </div>
      {passage.assessment && <p className="wr-dxd-opinion-t">{passage.assessment}</p>}
      {passage.rewrite_brief && <p className="wr-dxd-fix">AI 的改法：{passage.rewrite_brief}</p>}
      {passage.findings_count > 0 && <p className="wr-dxd-why">新看出的 {passage.findings_count} 条已并进下面的清单。</p>}
      {passage.rewrite_brief && pid != null && onRewriteParagraph && (
        <div className="wr-dxd-row-acts">
          <button type="button" className="btn btn-accent btn-sm" onClick={() => onRewriteParagraph(pid, passage.rewrite_brief, passage)}>
            <I.Sparkles size={13} /> 按这个改法改写这一段
          </button>
        </div>
      )}
    </section>
  );
}

function WrDeepDrawer({
  open, loading = false, error = null, onRetry,
  diagnosis, findings = [], activeKey, filter = "all", onFilter,
  showIgnored = false, onToggleIgnored,
  onPick, onIgnore, onRestore, onRescan, onSelect, onRewrite,
  aiBusy = false, aiError = null, onRunAi, onOpenSettings,
  onPassageReview, passageBusy = null, passageError = null, lastPassage = null, onRewriteParagraph, onLocateParagraph,
  handoffMiss = false,
  log, persistenceStatus = "idle", onClose,
}) {
  const asideRef = React.useRef(null);
  /* 收起时只是移出画面：标 inert，Tab 不会走进看不见的按钮 */
  useWrInert(asideRef, !open);
  const all = findings || [];
  const openList = all.filter((f) => !f.ignored);
  const ignoredList = all.filter((f) => f.ignored);
  const counts = WR_DX_SOURCE_ORDER.reduce((acc, key) => ({ ...acc, [key]: openList.filter((f) => f.source === key).length }), {});
  const visible = filter === "all" ? openList : openList.filter((f) => f.source === filter);
  const active = visible.find((f) => f.signal_id === activeKey) || null;
  const noText = !!(diagnosis && diagnosis.text && diagnosis.text.layer === "none");
  const syncNote = persistenceStatus === "loading" || persistenceStatus === "saving" ? "决定正在同步…"
    : persistenceStatus === "synced" ? "决定已同步到服务器。"
    : persistenceStatus === "local" ? "服务器暂时连不上，决定先存在本机，下次操作时再同步。"
    : "";
  const filterChips = [
    { key: "all", label: "全部", count: openList.length },
    ...WR_DX_SOURCE_ORDER.filter((key) => counts[key] > 0).map((key) => ({ key, label: WR_DX_SOURCES[key], count: counts[key] })),
  ];
  return (
    <aside ref={asideRef} className={`wr-drawer right wr-dxd ${open ? "show" : ""}`} aria-label="深改诊断">
      <header className="wr-drawer-head">
        <span className="wr-drawer-title"><I.Microscope size={15} /> 深改诊断 {openList.length} 项</span>
        <div className="wr-dxd-head-acts">
          <IconButton icon="Refresh" label="重新诊断本场" onClick={onRescan} disabled={loading} />
          <CloseButton className="wr-drawer-x" label="收起深改诊断" onClick={onClose} />
        </div>
      </header>

      <div className="wr-dxd-scroll">
        {loading && !diagnosis && (
          <div className="wr-dxd-loading" role="status"><Spinner size={15} /> 正在诊断本场…</div>
        )}
        {error && !diagnosis && (
          <Notice tone="danger" title="读不到本场的诊断"
            actions={onRetry ? <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>重试</button> : null}>
            {(error && error.message) || "服务器没有回答。"}
          </Notice>
        )}

        {diagnosis && !noText && (
          <DxAiBlock ai={diagnosis.ai} busy={aiBusy} error={aiError} onRun={onRunAi} onRetry={onRunAi} onOpenSettings={onOpenSettings} />
        )}

        {diagnosis && diagnosis.style_bound && (
          <Notice tone="info" className="wr-dxd-notice">
            {diagnosis.craft_calibration && diagnosis.craft_calibration.rules
              ? "本场绑定了参考画像：规则体检已按参考书校准——这位作者的常用词不当毛病，这位作者常态的检查只作提示；仍与样例冲突时以样例为准。"
              : "本场绑定了参考画像：规则体检是房风词表的意见，与参考作者的做法冲突时以样例为准。"}
            {diagnosis.craft_calibration && diagnosis.craft_calibration.note ? ` ${diagnosis.craft_calibration.note}` : ""}
          </Notice>
        )}
        {lastPassage && (
          <DxPassageNote passage={lastPassage} onRewriteParagraph={onRewriteParagraph} />
        )}
        {handoffMiss && (
          <Notice tone="warn" className="wr-dxd-notice">
            文学质量里指的那一处在当前作者稿里没有对应位置——它可能来自另一层文本（比如运行终稿），或已经改掉了。
          </Notice>
        )}

        {diagnosis && noText && (
          <div className="wr-dxd-clear">
            <I.FileText size={22} />
            <div className="wr-dxd-clear-t">这一场还没有正文</div>
            <p>先回到起草写几段，或在 AI 起草台起草，再来诊断。</p>
          </div>
        )}

        {diagnosis && !noText && openList.length === 0 && (
          <div className="wr-dxd-clear">
            <I.CheckCircle size={22} />
            <div className="wr-dxd-clear-t">本场没有发现待改的句段</div>
            <p>{diagnosis.ai && diagnosis.ai.status === "not_run" ? "规则没挑出什么；要更严格的一遍，跑一次 AI 深评。" : "可以回到起草姿态继续写，或者换一场再诊断。"}</p>
          </div>
        )}

        {diagnosis && !noText && openList.length > 0 && (
          <>
            {filterChips.length > 2 && (
              <div className="wr-dxd-filters" role="group" aria-label="按来源筛选">
                {filterChips.map((chip) => (
                  <button type="button" key={chip.key} className={`wr-dxd-chip ${filter === chip.key ? "is-on" : ""}`}
                    aria-pressed={filter === chip.key} onClick={() => onFilter && onFilter(chip.key)}>
                    {chip.label} <span className="wr-dxd-chip-n">{chip.count}</span>
                  </button>
                ))}
              </div>
            )}
            <ul className="wr-dxd-list">
              {visible.map((f) => (
                <li key={f.signal_id}>
                  <DxFindingRow finding={f} active={!!(active && active.signal_id === f.signal_id)} onPick={onPick} />
                  {active && active.signal_id === f.signal_id && (
                    <DxFindingDetail finding={f} onSelect={onSelect} onRewrite={onRewrite} onIgnore={onIgnore}
                      onPassageReview={onPassageReview} passageBusy={passageBusy} passageError={passageError} onOpenSettings={onOpenSettings}
                      onLocateParagraph={onLocateParagraph} />
                  )}
                </li>
              ))}
            </ul>
          </>
        )}

        {ignoredList.length > 0 && (
          <div className="wr-dxd-ignored">
            <button type="button" className="wr-dxd-ignored-toggle" aria-expanded={showIgnored} onClick={onToggleIgnored}>
              已忽略 {ignoredList.length} 项 <I.ChevronDown size={13} />
            </button>
            {showIgnored && (
              <ul className="wr-dxd-ignored-list">
                {ignoredList.map((f) => (
                  <li key={f.signal_id}>
                    <Tag tone={qSevTone(f.severity)}>{f.label || f.dimension}</Tag>
                    <span className="wr-dxd-ignored-t">{f.issue}</span>
                    <button type="button" className="btn btn-quiet btn-sm" onClick={() => onRestore(f)}>恢复</button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {log && log.length > 0 && (
          <div className="wr-dxd-log">
            <div className="wr-dxd-sub">最近的决定</div>
            <ul>
              {log.slice(0, 6).map((d, i) => (
                <li key={i} className="wr-dxd-dec">
                  <span className="wr-dxd-dec-t">{new Date(d.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</span>
                  <span className="wr-dxd-dec-x">{d.text}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="wr-dxd-note">
          诊断只指出问题、不替你改字：规则体检、节奏提示和起草台的评审随时可看，AI 深评要用模型。
          忽略过的条目不再提示，也不会再出现在文学质量和成稿中心里；点「已忽略」里的恢复可以找回。
          {syncNote ? <> {syncNote}</> : null}
        </div>
      </div>
    </aside>
  );
}

export {
  wrDeepMark, wrDeepUnmark, wrDxRangeFor, wrDxFetch, wrDxRunAi, wrDxReviewPassage, wrDxWithIgnored,
  wrDxLog, wrDxPushLog, wrDxAddSkip, wrDxRemoveSkip, wrDxClearSkips, wrDxSkips, wrDxSnapshot,
  wrDxApplyPreferences, wrDxMergePreferences, wrDxLoadPreferences, wrDxSavePreferences,
  WR_DX_SOURCES, WrDeepDrawer,
};
