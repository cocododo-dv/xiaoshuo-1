import React from "react";
import { I } from "./icons.jsx";
import { CloseButton, IconButton, Notice, Spinner, Tag, cx } from "./ws-ui.jsx";
import { wrAiError } from "./ws-writer-ai.js";
import { sevClass, wrDeepMark, wrDeepUnmark, wrDxRangeFor } from "./ws-deep-marks.js";
import {
  wrDxAddSkip, wrDxApplyPreferences, wrDxFetch, wrDxLoadPreferences, wrDxLog, wrDxMergePreferences, wrDxPushLog,
  wrDxRemoveSkip, wrDxReviewPassage, wrDxRunAi, wrDxSavePreferences, wrDxSkips, wrDxSnapshot, wrDxWithIgnored,
} from "./ws-deep-prefs.js";
import { useWrInert } from "./ws-writer-hooks.js";
import { FINDING_SEVERITY_ORDER, findingLabel, findingSeverityLabel } from "./labels/finding.js";
import { DxFold, DxSource } from "./ws-deep-parts.jsx";
import { DxStyleBlock } from "./ws-deep-style.jsx";
import { formatClockTime, formatLocaleMonthDayTime } from "./lib/format.js";

/* ==========================================================
   ws-deep — 写作台深改面板（2026-09-22 场景诊断统一）
   ----------------------------------------------------------
   诊断只有一份，在服务端：GET /api/v1/scenes/{id}/deep-review 把规则维度体检（QUALITY_DIMENSIONS）、段落节奏
   （贴邻叠句 / 段落偏长 / 句首重复——原先是这里三条本地正则）、起草台的准定稿评审、AI 深评和风格参考的
   对照检查（风格对照，2026-10-20；那一行在 ws-deep-style.jsx）、全书的跨场重复（成稿中心「跨章」页签是同一份）
   合成同一种发现形状（signal_id / source / dimension / severity / issue / recommendation /
   evidence{paragraph_index,start,end,excerpt}），成稿中心「通读」页签里各场的计数与落到场里的发现读的也是这份，
   所以一条发现从那边点过来，这里就是同一条（文学质量页 2026-10-20 退役）。
   · wrDxFetch / wrDxRunAi     取诊断 / 跑「AI 深评」（POST 同一路径；无模型时后端 409，这里给「去系统设置」）
   · wrDeepMark / wrDeepUnmark 按发现的段落序号 + 证据文字在编辑器里标高亮
   · WrDeepDrawer              右栏面板（2026-10-21 重排：第一屏留给发现清单，不再是一摞工具与说明压在上面）——
                               顶部：标题、开着的条数、决定没同步上时的「未同步」、重新诊断；
                               来源状态条：AI 深评 / 风格对照各一行（新旧、分数、跑一次），整体建议与参考书校准折叠；
                               清单：来源下拉、按严重程度分组；展开的一条与它那一行在同一张卡里（证据 / 改法 / AI 的判断），
                               只有一个主按钮：AI 复核过、认为成立并给了这一处的改法就按 AI 的，否则按诊断的；
                               底部：已忽略、最近的决定（都折叠）。
   深改只诊断、不改字：每一条给「选中去改」/「按诊断改写」——回到起草姿态、选中那一句，
   改写走选区工具条那条真实的改写接口，并把这条发现（id / 维度 / 改法）一起发给后端。
   忽略按 signal_id 记在服务端（deep-review/preferences，带修订号），成稿门也认。
   姿态本身（进出、偏好同步、选中去改写）在 ws-writer-deep-posture.js；本机偏好与诊断请求在
   ws-deep-prefs.js，编辑器里的标注在 ws-deep-marks.js（2026-09-29 拆出，名字照旧从这里转出），
   折叠与来源行这两样小件在 ws-deep-parts.jsx。
   ========================================================== */

const WR_DX_SOURCES = {
  rules: "规则",
  craft: "节奏",
  review: "起草评审",
  ai: "AI 深评",
  style: "风格对照",
  repeat: "跨场重复",
};
const WR_DX_SOURCE_ORDER = ["rules", "craft", "review", "ai", "style", "repeat"];
const WR_DX_LENS = { story: "故事", character: "人物", prose: "文字", reader: "读者", theme: "主题" };
const WR_DX_ORIGIN = { passage: "出自 AI 局部深评", chapter: "出自 AI 通读本章" };
const WR_DX_VERDICT = {
  holds: { label: "AI：成立", tone: "accent" },
  partly: { label: "AI：部分成立", tone: "warn" },
  does_not_hold: { label: "AI：不成立", tone: "ok" },
  no_finding: { label: "AI：没有要改的", tone: "ok" },
};

/* 严重程度的组：阻断 → 修订 → 审美 → 信息；认不出的归「信息」（编辑器里的标注也按「信息」上色） */
const severityOf = (finding) => (FINDING_SEVERITY_ORDER.includes(finding && finding.severity) ? finding.severity : "info");

/* 清单按严重程度排，组内保持服务端的次序（段落、来源）。服务端本来就这样排，这里保证分组显示、
   「忽略后停到下一条」与从对照检查跳进来时「停在那个来源的第一条」用的是同一个次序 */
function wrDxBySeverity(list) {
  return (list || [])
    .map((finding, index) => ({ finding, index, rank: FINDING_SEVERITY_ORDER.indexOf(severityOf(finding)) }))
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map((entry) => entry.finding);
}

/* ==========================================================
   WrDeepDrawer — 写作台右栏 · 深改面板
   ========================================================== */
/* 深评失败：无模型（后端 409 + author_action）给「去系统设置」，其余给重试（与写作台其他 AI 入口同一套翻译）；
   没有字可看（WRITER_*_NO_TEXT）只说一句、不给重试——再点一次结果也一样 */
function DxAiError({ error, onRetry, onOpenSettings }) {
  const info = wrAiError(error);
  const configOnly = info.kind === "config";
  const quiet = configOnly || info.kind === "no-text";
  const retry = !configOnly && onRetry && info.actionLabel
    ? <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>{info.actionLabel}</button> : null;
  const settings = info.offersSettings && onOpenSettings
    ? <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenSettings}>去系统设置</button> : null;
  return (
    <Notice tone={quiet ? "warn" : "danger"} className="wr-dxd-notice" actions={retry || settings ? <>{retry}{settings}</> : null}>
      {info.message}
    </Notice>
  );
}

/* 来源状态条里 AI 深评那一行：只说新旧与总分；整体建议折叠——这次打开面板里刚跑完一轮（换了一份深评）就展开，
   作者刚要的就是它 */
function DxAiBlock({ ai, busy, error, onRun, onRetry, onOpenSettings }) {
  const status = (ai && ai.status) || "not_run";
  const score = ai && ai.overall_score != null ? Math.round(ai.overall_score * 100) : null;
  const whenText = formatLocaleMonthDayTime(ai && ai.created_at);
  const brief = status !== "not_run" && ai && Array.isArray(ai.revision_brief) ? ai.revision_brief.slice(0, 4) : [];
  const evaluationId = (ai && ai.evaluation_id) || null;
  const [briefOpen, setBriefOpen] = React.useState(false);
  const seen = React.useRef(evaluationId);
  React.useEffect(() => {
    if (evaluationId && seen.current !== evaluationId) setBriefOpen(true);
    seen.current = evaluationId;
  }, [evaluationId]);
  const meta = status === "current"
    ? [whenText, score != null ? `总分 ${score}` : ""].filter(Boolean).join(" · ") || "对着现在这份正文"
    : status === "stale"
      ? `${whenText ? `${whenText} 的` : "这份"}深评是改前的：证据找不到的会标出来，要按现在的稿子判断就重新深评`
      : "还没跑过：模型按十个文学维度、五个镜头读本场";
  return (
    <DxSource icon="Sparkles" name="AI 深评" className="wr-dxd-ai" meta={meta} stale={status === "stale"}
      action={(
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={onRun}
          title="让模型读一遍本场，发现并进下面的清单（要用模型）">
          {busy ? <Spinner size={13} /> : <I.Sparkles size={13} />} {busy ? "深评中…" : (status === "not_run" ? "开始深评" : "重新深评")}
        </button>
      )}>
      {error && <DxAiError error={error} onRetry={onRetry} onOpenSettings={onOpenSettings} />}
      {brief.length > 0 && (
        <DxFold label={`整体建议 ${brief.length} 条`} open={briefOpen} onToggle={() => setBriefOpen((value) => !value)}>
          <ul className="wr-dxd-brief">
            {brief.map((line, i) => (
              <li key={i}>{line.action || line.recommendation || line.text || ""}</li>
            ))}
          </ul>
        </DxFold>
      )}
    </DxSource>
  );
}

/* 有参考绑定时规则体检的口径：一行说校准了没有、本场放过几个词，展开看全文 */
function DxCalibration({ diagnosis }) {
  const calibration = diagnosis.craft_calibration || null;
  const calibrated = !!(calibration && calibration.rules);
  const waived = calibration && Array.isArray(calibration.waived_in_scene) ? calibration.waived_in_scene : [];
  const label = calibrated
    ? `规则已按参考书校准${waived.length ? ` · 本场放过 ${waived.length} 个词` : ""}`
    : "规则未按参考书校准";
  return (
    <div className="wr-dxd-source">
      <DxFold label={label}>
        <p className="wr-dxd-calib">
          {calibrated
            ? "本场绑定了参考画像：规则体检已按参考书校准——词表词按这位作者的密度判，这位作者常态的检查只作提示；仍与样例冲突时以样例为准。"
            : "本场绑定了参考画像：规则体检是房风词表的意见，与参考作者的做法冲突时以样例为准。"}
          {calibration && calibration.note ? ` ${calibration.note}` : ""}
          {waived.length > 0 && (
            <span data-testid="dx-waived">
              {" "}本场按这位作者的密度放过 {waived.length} 个词：
              {waived.slice(0, 6).map((item) => `${item.term} ×${item.count}`).join("、")}
              {waived.length > 6 ? "…" : ""}。
            </span>
          )}
        </p>
      </DxFold>
    </div>
  );
}

/* 跨段发现（局部深评对照全场看出的矛盾 / 重复 / 承接）：另一段的段号与关系 */
function relatedTag(finding) {
  const related = finding && finding.related;
  if (!related) return "";
  const where = Number.isInteger(related.paragraph_index) ? `与第 ${related.paragraph_index + 1} 段` : "与另一段";
  return `${where}${related.label || "矛盾"}`;
}

function calibratedLabel(finding) {
  const calibrated = finding && finding.calibrated;
  if (!calibrated || !calibrated.kind) return "";
  if (calibrated.kind === "profile_deliberate_repetition") return "画像：刻意重复";
  return calibrated.level === "common" ? "参考作者也常见" : "参考作者的常态";
}

/* 一行的说明：维度 · 来源 · 镜头（· 整场）。维度与来源同名（跨场重复）只说一次 */
function rowMeta(finding) {
  const src = WR_DX_SOURCES[finding.source] || finding.source || "";
  const label = findingLabel(finding);
  const lens = finding.lens && WR_DX_LENS[finding.lens] ? WR_DX_LENS[finding.lens] : "";
  const whole = !finding.evidence && !finding.stale ? "整场" : "";
  return [label !== src ? label : "", src, lens, whole].filter(Boolean).join(" · ");
}

/* 一行最多一个标记，按要紧的先：证据没了 > AI 的判断 > 跨段 > 参考书校准。没轮上的在展开里说 */
function rowBadge(finding) {
  if (finding.stale) return { key: "stale", label: "证据已不在正文里", tone: "warn" };
  const verdict = finding.opinion && WR_DX_VERDICT[finding.opinion.verdict];
  if (verdict) return { key: "opinion", label: verdict.label, tone: verdict.tone };
  if (finding.related) return { key: "related", label: relatedTag(finding), tone: "warn" };
  const calibrated = calibratedLabel(finding);
  if (calibrated) return { key: "calibrated", label: calibrated, tone: "info" };
  return null;
}

/* 一行：左边一道严重程度色条（与正文里的标注同色、宽度一样，标题都从同一处起），标题，下面一行说明 + 一个标记。
   再点一下开着的那一行就收起它 */
function DxFindingRow({ finding, active, onPick }) {
  const badge = rowBadge(finding);
  return (
    <button type="button" className="wr-dxd-row" aria-pressed={active} onClick={() => onPick(finding.signal_id)}>
      <span className={`wr-dxd-sev ${sevClass(finding.severity)}`} aria-hidden="true" />
      <span className="wr-dxd-body">
        <span className="wr-dxd-t">{finding.issue}</span>
        <span className="wr-dxd-h">
          <span>{rowMeta(finding)}</span>
          {badge && <Tag tone={badge.tone} className="wr-dxd-badge">{badge.label}</Tag>}
        </span>
      </span>
    </button>
  );
}

/* 「AI 看这一处」对这条发现的判断：成立 / 部分成立 / 不成立 + 评语 + 这一处的改法（按它改写是这一条的主按钮） */
function DxOpinion({ opinion }) {
  const meta = WR_DX_VERDICT[opinion.verdict] || WR_DX_VERDICT.no_finding;
  return (
    <div className="wr-dxd-opinion">
      <div className="wr-dxd-opinion-head">
        <Tag tone={meta.tone}>{meta.label}</Tag>
        {opinion.status === "stale" && <span className="wr-dxd-stale">改前的判断</span>}
      </div>
      {opinion.assessment && <p className="wr-dxd-opinion-t">{opinion.assessment}</p>}
      {opinion.rewrite_brief && <p className="wr-dxd-fix">AI 的改法：{opinion.rewrite_brief}</p>}
    </div>
  );
}

/* 展开的一条：证据、跨段的另一句、改法、AI 的判断、为什么（折叠）、出处；动作两行——
   改：一个主按钮 +「选中去改」；判：「AI 看这一处」+ 忽略（AI 判了不成立就叫「按 AI 的判断忽略」） */
function DxFindingDetail({ finding, onSelect, onRewrite, onIgnore, onPassageReview, passageBusy, passageError, onOpenSettings, onLocateParagraph }) {
  const ev = finding.evidence;
  const canSelect = !!(ev && ev.excerpt && onSelect && !finding.stale);
  /* 服务端只钉到段（偏移为空）、或整段就是证据（段落偏长）：选中的是一段 */
  const paragraphLevel = !!(ev && (ev.start == null || ev.end == null || finding.dimension === "long_paragraph"));
  const reviewing = passageBusy === finding.signal_id;
  const canReview = !!(ev && onPassageReview && !finding.stale);
  const reviewError = passageError && passageError.key === finding.signal_id ? passageError.error : null;
  const related = finding.related || null;
  const relatedIndex = related && Number.isInteger(related.paragraph_index) ? related.paragraph_index : null;
  const opinion = finding.opinion || null;
  const aiBrief = opinion && opinion.rewrite_brief && opinion.verdict !== "does_not_hold" ? opinion.rewrite_brief : "";
  const primary = canSelect && onRewrite
    ? (aiBrief
      ? { label: "按 AI 的改法改写", run: () => onRewrite(finding, { instruction: aiBrief }) }
      : (finding.recommendation ? { label: "按诊断改写", run: () => onRewrite(finding) } : null))
    : null;
  const origin = finding.origin && WR_DX_ORIGIN[finding.origin.kind]
    ? `${WR_DX_ORIGIN[finding.origin.kind]}${finding.origin.carried_from ? "，沿用上次的结果" : ""}` : "";
  const badge = rowBadge(finding);
  const calibrated = badge && badge.key === "calibrated" ? "" : calibratedLabel(finding);
  const provenance = [origin, calibrated].filter(Boolean).join(" · ");
  return (
    <div className="wr-dxd-detail">
      {(ev && ev.excerpt) ? <blockquote className="wr-dxd-ev">{ev.excerpt}</blockquote>
        : (finding.context ? <blockquote className="wr-dxd-ev">{finding.context}</blockquote> : null)}
      {related && (
        <div className="wr-dxd-related">
          <div className="wr-dxd-related-head">
            <span className="wr-dxd-sub">{relatedTag(finding)}{related.stale ? "（那句已不在正文里）" : ""}</span>
            {relatedIndex != null && onLocateParagraph && (
              <button type="button" className="btn btn-quiet btn-xs" onClick={() => onLocateParagraph(relatedIndex)}>看第 {relatedIndex + 1} 段</button>
            )}
          </div>
          <blockquote className="wr-dxd-ev">{related.excerpt}</blockquote>
        </div>
      )}
      {finding.recommendation && <p className="wr-dxd-fix">改法：{finding.recommendation}</p>}
      {opinion && <DxOpinion opinion={opinion} />}
      {finding.why && (
        <DxFold label="为什么">
          <p className="wr-dxd-why">{finding.why}</p>
        </DxFold>
      )}
      {provenance && <p className="wr-dxd-why">{provenance}</p>}
      {reviewError && <DxAiError error={reviewError} onRetry={() => onPassageReview(finding)} onOpenSettings={onOpenSettings} />}
      <div className="wr-dxd-acts">
        {(primary || canSelect) && (
          <div className="wr-dxd-row-acts">
            {primary && (
              <button type="button" className="btn btn-accent btn-sm" onClick={primary.run}>
                <I.Sparkles size={13} /> {primary.label}
              </button>
            )}
            {canSelect && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => onSelect(finding)}
                title={paragraphLevel ? "回到起草，选中这一段，用选区工具条去改" : "回到起草，选中这一句，用选区工具条去改"}>
                <I.Pen size={13} /> 选中去改
              </button>
            )}
          </div>
        )}
        <div className="wr-dxd-row-acts">
          {canReview && (
            <button type="button" className="btn btn-quiet btn-sm" disabled={!!passageBusy} onClick={() => onPassageReview(finding)}
              title="让模型只看这一处：这条发现成不成立、这一处怎么改">
              {reviewing ? <Spinner size={13} /> : <I.Sparkles size={13} />} {reviewing ? "AI 在看…" : "AI 看这一处"}
            </button>
          )}
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => onIgnore(finding)}>
            {opinion && opinion.verdict === "does_not_hold" ? "按 AI 的判断忽略" : "忽略这一项"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* 独立看一段 / 一段范围（没有复核某条发现）的结果：判定 + 评语 + 这一处的改法；新发现已并进清单。
   模型看的是整场（焦点段标出），所以「看了第 2–3 段」也可能指出与别处的矛盾。
   给了改法时：一段给「按这个改法改写这一段」，几段给「按这个改法改写这几段」（改写按段送、按段换回，重评 R12）。
   放在清单最上面，看完可以收起。 */
function DxPassageNote({ passage, onRewriteParagraph, onDismiss }) {
  if (!passage || passage.about_signal_id) return null;
  const meta = WR_DX_VERDICT[passage.verdict] || WR_DX_VERDICT.no_finding;
  const focus = Array.isArray(passage.focus_paragraphs) && passage.focus_paragraphs.length
    ? passage.focus_paragraphs
    : (Number.isInteger(passage.paragraph_index) ? [passage.paragraph_index] : []);
  const pid = focus.length === 1 ? focus[0] : null;
  const first = focus.length ? Math.min(...focus) : null;
  const last = focus.length ? Math.max(...focus) : null;
  const where = focus.length > 1 ? `第 ${focus[0] + 1}–${focus[focus.length - 1] + 1} 段` : (pid != null ? `第 ${pid + 1} 段` : "这一段");
  return (
    <section className="wr-dxd-passage" aria-label="AI 看这一段">
      <div className="wr-dxd-opinion-head">
        <span className="wr-dxd-sub"><I.Sparkles size={13} /> AI 看了{where}</span>
        <Tag tone={meta.tone}>{meta.label}</Tag>
        {passage.status === "stale" && <span className="wr-dxd-stale">改前的判断</span>}
        {onDismiss && <CloseButton size="xs" className="wr-dxd-passage-x" label="收起这条结果" onClick={onDismiss} />}
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
      {passage.rewrite_brief && focus.length > 1 && onRewriteParagraph && (
        <div className="wr-dxd-row-acts">
          <button type="button" className="btn btn-accent btn-sm" onClick={() => onRewriteParagraph(first, passage.rewrite_brief, passage, last)}>
            <I.Sparkles size={13} /> 按这个改法改写这几段
          </button>
        </div>
      )}
    </section>
  );
}

/* deep：useDeepPosture 的返回整个传进来（过去是从它拆出来的 27 个 prop） */
function WrDeepDrawer({ deep, open, onClose, onOpenSettings }) {
  const {
    loading = false, error = null, reload: onRetry,
    diagnosis, findings = [], activeKey, filter = "all", setFilter: onFilter,
    showIgnored = false, toggleIgnored: onToggleIgnored,
    pick: onPick, ignore: onIgnore, restore: onRestore, rescan: onRescan,
    selectForRewrite: onSelect, rewriteFromFinding: onRewrite,
    aiBusy = false, aiError = null, runAi: onRunAi,
    reviewPassage: onPassageReview, passageBusy = null, passageError = null, lastPassage = null, dismissPassage: onDismissPassage,
    rewriteParagraph: onRewriteParagraph, locateParagraph: onLocateParagraph,
    handoffMiss = false,
    log, persistenceStatus = "idle",
  } = deep || {};
  const asideRef = React.useRef(null);
  const filterId = React.useId();
  /* 收起时只是移出画面：标 inert，Tab 不会走进看不见的按钮 */
  useWrInert(asideRef, !open);
  const all = findings || [];
  const openList = all.filter((f) => !f.ignored);
  const ignoredList = all.filter((f) => f.ignored);
  const counts = WR_DX_SOURCE_ORDER.reduce((acc, key) => ({ ...acc, [key]: openList.filter((f) => f.source === key).length }), {});
  const visible = filter === "all" ? openList : openList.filter((f) => f.source === filter);
  const active = visible.find((f) => f.signal_id === activeKey) || null;
  const groups = FINDING_SEVERITY_ORDER
    .map((severity) => ({ severity, items: visible.filter((f) => severityOf(f) === severity) }))
    .filter((group) => group.items.length > 0);
  const noText = !!(diagnosis && diagnosis.text && diagnosis.text.layer === "none");
  /* 来源下拉：只有一个来源时不出（当前筛着的来源一条都不剩了也留着，好筛回「全部」） */
  const sourceOptions = [
    { key: "all", label: "全部", count: openList.length },
    ...WR_DX_SOURCE_ORDER.filter((key) => counts[key] > 0 || key === filter).map((key) => ({ key, label: WR_DX_SOURCES[key], count: counts[key] })),
  ];
  const showFilter = sourceOptions.length > 2 || filter !== "all";
  return (
    <aside ref={asideRef} className={`wr-drawer right wr-dxd ${open ? "show" : ""}`} aria-label="深改诊断">
      <header className="wr-drawer-head">
        <span className="wr-drawer-title"><I.Microscope size={15} /> 深改诊断 {openList.length} 项</span>
        {persistenceStatus === "saving" && <Spinner size={12} label="决定正在同步…" className="wr-dxd-sync" />}
        {persistenceStatus === "local" && (
          <Tag tone="warn" className="wr-dxd-sync" title="服务器暂时连不上，决定先存在本机，下次操作时再同步。">未同步</Tag>
        )}
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
          <div className="wr-dxd-sources">
            <DxAiBlock ai={diagnosis.ai} busy={aiBusy} error={aiError} onRun={onRunAi} onRetry={onRunAi} onOpenSettings={onOpenSettings} />
            {diagnosis.style && <DxStyleBlock style={diagnosis.style} sceneId={diagnosis.scene_id} onChecked={onRescan} />}
            {diagnosis.style_bound && <DxCalibration diagnosis={diagnosis} />}
          </div>
        )}

        {handoffMiss && (
          <Notice tone="warn" className="wr-dxd-notice">
            跳过来的那一处在当前作者稿里没有对应位置——它可能来自另一层文本（比如运行终稿），或已经改掉了。
          </Notice>
        )}
        {/* 独立看一段 / 几段（深改工具条「AI 看这一段」）没成：照实说（没配模型 → 去系统设置；那一段是空的 → 说没有字）。
            复核某条发现的失败挂在那条发现下面（DxFindingDetail）；过去独立看一段的失败哪里都不显示 */}
        {passageError && String(passageError.key || "").startsWith("para:") && (
          <DxAiError error={passageError.error} onOpenSettings={onOpenSettings} />
        )}
        {lastPassage && (
          <DxPassageNote passage={lastPassage} onRewriteParagraph={onRewriteParagraph} onDismiss={onDismissPassage} />
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
          <div className="wr-dxd-findings">
            {showFilter && (
              <div className="wr-dxd-bar">
                <label className="wr-dxd-bar-l" htmlFor={filterId}>来源</label>
                <select id={filterId} className="select wr-dxd-filter" value={filter} onChange={(event) => onFilter && onFilter(event.target.value)}>
                  {sourceOptions.map((option) => (
                    <option key={option.key} value={option.key}>{option.label} {option.count}</option>
                  ))}
                </select>
              </div>
            )}
            {groups.map((group) => (
              <section key={group.severity} className="wr-dxd-group" aria-label={`${findingSeverityLabel(group.severity)} ${group.items.length} 项`}>
                <h3 className="wr-dxd-group-h">
                  <span className={`wr-dxd-sev ${sevClass(group.severity)}`} aria-hidden="true" />
                  {findingSeverityLabel(group.severity)}<span className="wr-dxd-group-n">{group.items.length}</span>
                </h3>
                <ul className="wr-dxd-list">
                  {group.items.map((f) => {
                    const on = !!(active && active.signal_id === f.signal_id);
                    return (
                      <li key={f.signal_id} className={cx("wr-dxd-item", on && "is-active")}>
                        <DxFindingRow finding={f} active={on} onPick={onPick} />
                        {on && (
                          <DxFindingDetail finding={f} onSelect={onSelect} onRewrite={onRewrite} onIgnore={onIgnore}
                            onPassageReview={onPassageReview} passageBusy={passageBusy} passageError={passageError} onOpenSettings={onOpenSettings}
                            onLocateParagraph={onLocateParagraph} />
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
            {visible.length === 0 && <p className="wr-dxd-hint">这个来源下没有开着的发现。</p>}
          </div>
        )}

        {(ignoredList.length > 0 || (log && log.length > 0)) && (
          <div className="wr-dxd-foot">
            {ignoredList.length > 0 && (
              <DxFold label={`已忽略 ${ignoredList.length} 项`} open={showIgnored} onToggle={onToggleIgnored}>
                <p className="wr-dxd-hint">忽略的不再提示，也不算进成稿中心与主页的计数；点「恢复」可以找回。</p>
                <ul className="wr-dxd-ignored-list">
                  {ignoredList.map((f) => (
                    <li key={f.signal_id}>
                      <span className={`wr-dxd-sev ${sevClass(f.severity)}`} aria-hidden="true" />
                      <span className="wr-dxd-ignored-t" title={`${findingLabel(f)}：${f.issue}`}>{f.issue}</span>
                      <button type="button" className="btn btn-quiet btn-sm" onClick={() => onRestore(f)}>恢复</button>
                    </li>
                  ))}
                </ul>
              </DxFold>
            )}
            {log && log.length > 0 && (
              <DxFold label="最近的决定">
                <ul className="wr-dxd-log">
                  {log.slice(0, 6).map((d, i) => (
                    <li key={i} className="wr-dxd-dec">
                      <span className="wr-dxd-dec-t">{formatClockTime(d.at)}</span>
                      <span className="wr-dxd-dec-x">{d.text}</span>
                    </li>
                  ))}
                </ul>
              </DxFold>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

export {
  wrDeepMark, wrDeepUnmark, wrDxRangeFor, wrDxFetch, wrDxRunAi, wrDxReviewPassage, wrDxWithIgnored, wrDxBySeverity,
  wrDxLog, wrDxPushLog, wrDxAddSkip, wrDxRemoveSkip, wrDxSkips, wrDxSnapshot,
  wrDxApplyPreferences, wrDxMergePreferences, wrDxLoadPreferences, wrDxSavePreferences,
  WR_DX_SOURCES, WrDeepDrawer,
};
