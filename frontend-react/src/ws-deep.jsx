import React from "react";
import { I } from "./icons.jsx";
import { CloseButton, IconButton, Notice, Spinner, Tag } from "./ws-ui.jsx";
import { wrAiError } from "./ws-writer-ai.js";
import { sevClass, wrDeepMark, wrDeepUnmark, wrDxRangeFor } from "./ws-deep-marks.js";
import {
  wrDxAddSkip, wrDxApplyPreferences, wrDxFetch, wrDxLoadPreferences, wrDxLog, wrDxMergePreferences, wrDxPushLog,
  wrDxRemoveSkip, wrDxReviewPassage, wrDxRunAi, wrDxSavePreferences, wrDxSkips, wrDxSnapshot, wrDxWithIgnored,
} from "./ws-deep-prefs.js";
import { useWrInert } from "./ws-writer-hooks.js";
import { findingLabel, findingSeverityLabel, findingSeverityTone } from "./labels/finding.js";
import { formatClockTime, formatLocaleMonthDayTime } from "./lib/format.js";

/* ==========================================================
   ws-deep — 写作台深改面板（2026-09-22 场景诊断统一）
   ----------------------------------------------------------
   诊断只有一份，在服务端：GET /api/v1/scenes/{id}/deep-review 把规则维度体检（QUALITY_DIMENSIONS）、段落节奏
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
   姿态本身（进出、偏好同步、选中去改写）在 ws-writer-deep-posture.js；本机偏好与诊断请求在
   ws-deep-prefs.js，编辑器里的标注在 ws-deep-marks.js（2026-09-29 拆出，名字照旧从这里转出）。
   ========================================================== */

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

function DxAiBlock({ ai, busy, error, onRun, onRetry, onOpenSettings }) {
  const status = (ai && ai.status) || "not_run";
  const score = ai && ai.overall_score != null ? Math.round(ai.overall_score * 100) : null;
  const whenText = formatLocaleMonthDayTime(ai && ai.created_at);
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
      <span className={`wr-dxd-mark ${sevClass(finding.severity)}`} title={`严重程度：${findingSeverityLabel(finding.severity)}`}>{findingLabel(finding)}</span>
      <span className="wr-dxd-body">
        <span className="wr-dxd-t">{finding.issue}</span>
        <span className="wr-dxd-h">
          <span className="wr-dxd-src">{src}{finding.lens && WR_DX_LENS[finding.lens] ? ` · ${WR_DX_LENS[finding.lens]}` : ""}{finding.origin && WR_DX_ORIGIN[finding.origin.kind] ? ` · ${WR_DX_ORIGIN[finding.origin.kind]}` : ""}{carried ? "（沿用上次）" : ""}</span>
          {finding.related && <span className="wr-dxd-related-tag">{relatedTag(finding)}</span>}
          {calibrated && <span className="wr-dxd-calibrated">{finding.calibrated.kind === "profile_deliberate_repetition" ? "画像：刻意重复" : (finding.calibrated.level === "common" ? "参考作者也常见" : "参考作者的常态")}</span>}
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

/* deep：useDeepPosture 的返回整个传进来（过去是从它拆出来的 27 个 prop） */
function WrDeepDrawer({ deep, open, onClose, onOpenSettings }) {
  const {
    loading = false, error = null, reload: onRetry,
    diagnosis, findings = [], activeKey, filter = "all", setFilter: onFilter,
    showIgnored = false, toggleIgnored: onToggleIgnored,
    pick: onPick, ignore: onIgnore, restore: onRestore, rescan: onRescan,
    selectForRewrite: onSelect, rewriteFromFinding: onRewrite,
    aiBusy = false, aiError = null, runAi: onRunAi,
    reviewPassage: onPassageReview, passageBusy = null, passageError = null, lastPassage = null,
    rewriteParagraph: onRewriteParagraph, locateParagraph: onLocateParagraph,
    handoffMiss = false,
    log, persistenceStatus = "idle",
  } = deep || {};
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
              ? "本场绑定了参考画像：规则体检已按参考书校准——词表词按这位作者的密度判，这位作者常态的检查只作提示；仍与样例冲突时以样例为准。"
              : "本场绑定了参考画像：规则体检是房风词表的意见，与参考作者的做法冲突时以样例为准。"}
            {diagnosis.craft_calibration && diagnosis.craft_calibration.note ? ` ${diagnosis.craft_calibration.note}` : ""}
            {diagnosis.craft_calibration && Array.isArray(diagnosis.craft_calibration.waived_in_scene) && diagnosis.craft_calibration.waived_in_scene.length > 0 && (
              <span className="wr-dxd-waived" data-testid="dx-waived">
                {" "}本场按这位作者的密度放过 {diagnosis.craft_calibration.waived_in_scene.length} 个词：
                {diagnosis.craft_calibration.waived_in_scene.slice(0, 6).map((item) => `${item.term} ×${item.count}`).join("、")}
                {diagnosis.craft_calibration.waived_in_scene.length > 6 ? "…" : ""}。
              </span>
            )}
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
                    <Tag tone={findingSeverityTone(f.severity)}>{findingLabel(f)}</Tag>
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
                  <span className="wr-dxd-dec-t">{formatClockTime(d.at)}</span>
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
  wrDxLog, wrDxPushLog, wrDxAddSkip, wrDxRemoveSkip, wrDxSkips, wrDxSnapshot,
  wrDxApplyPreferences, wrDxMergePreferences, wrDxLoadPreferences, wrDxSavePreferences,
  WR_DX_SOURCES, WrDeepDrawer,
};
