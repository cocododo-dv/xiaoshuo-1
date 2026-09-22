import React from "react";
import { I } from "./icons.jsx";
import { useCatalogChapters } from "./ws-catalog.jsx";
import { EmptyState, Notice, PageHeader, Segmented, Spinner, StatTile, Tag } from "./ws-ui.jsx";
import { chapterLabel, chapterLabelById, findSceneByBackendId } from "./ws-labels.js";
import {
  QUALITY_DIMS, QUALITY_DIM_KEYS, QUALITY_MIN_SEVERITIES, QUALITY_SEV, QUALITY_TEXT_LAYERS, Q_ITEM_LAYER,
  qDimLabel, qFindingText, qObjectLabel, qPct, qPlainText, qRiskDims, qScore, qSevLabel, qSevTone,
} from "./ws-quality-model.js";
import {
  qAnalyzeText, qChapterSetReview, qLoadOverview, qScopeFilters, qSnapshot, useQualityState,
} from "./ws-quality-store.js";

/* ==========================================================
   WsQuality — 文学质量巡检
   对接后端 21 维「质量地板」引擎：
     GET  /api/v1/literary-quality/overview            全库巡检
     POST /api/v1/literary-quality/analyze-text        临时文本即时扫描
     POST /api/v1/literary-quality/chapter-set-review  章组复审
   引擎是纯规则 / 词表打分，与是否启用模型无关——随时可用。

   这个文件只管页面（巡检 / 章组复审两个视图与它们的条目）；21 维与严重度的中文名、
   格式化在 ws-quality-model.js，请求与缓存在 ws-quality-store.js。
   ========================================================== */

function WsQuality({ go }) {
  const [tab, setTab] = React.useState("overview");
  // 筛选与草稿放在这一层：切去「章组复审」再切回来不丢、也不重新巡检
  const [filters, setFilters] = React.useState({ text_layer: "author_draft_preferred", chapter_id: "", risk_type: "", min_severity: "" });
  const [draft, setDraft] = React.useState("");

  // 初次进入巡检一轮（按当前作品作用域，作者只看自己作品）
  React.useEffect(() => { qLoadOverview(qScopeFilters(filters)); /* eslint-disable-next-line */ }, []);

  return (
    <div className="ws-page ws-view q-quality" data-screen-label="quality">
      <PageHeader
        title="文学质量"
        description="用 21 个维度的规则引擎巡检稿件，或即时扫描一段文字，找出模型腔、意象同质、无抉择场景这类问题。巡检不调用模型，随时可用。"
      />
      <Segmented
        className="q-tabs"
        label="文学质量视图"
        value={tab}
        onChange={setTab}
        options={[{ value: "overview", label: "稿件巡检" }, { value: "review", label: "章组复审" }]}
      />
      {tab === "overview"
        ? <QualityOverview go={go} filters={filters} setFilters={setFilters} draft={draft} setDraft={setDraft} />
        : <QualityChapterSet go={go} />}
    </div>
  );
}

function QualityOverview({ go, filters, setFilters, draft, setDraft }) {
  const st = useQualityState();
  const chapters = useCatalogChapters() || [];

  const reload = () => qLoadOverview(qScopeFilters(filters));
  const overviewFailed = !!(st.error && st.errorScope === "overview");
  const setF = (k, v) => setFilters((f) => ({ ...f, [k]: v }));

  const ov = st.overview || {};
  const summary = ov.summary || {};
  const items = ov.items || [];
  const analyze = st.analyze;
  const chapterOptions = chapters.filter((c) => c && c.backendId);

  const summaryCards = [
    { k: "object_count", label: "巡检对象", v: summary.object_count ?? 0 },
    { k: "mean_score", label: "平均分", v: summary.mean_score == null ? "—" : qPct(summary.mean_score) },
    { k: "high_risk_count", label: "高风险项", v: summary.high_risk_count ?? 0, tone: summary.high_risk_count ? "warn" : undefined },
    { k: "model_voice_count", label: "模型腔", v: summary.model_voice_count ?? 0 },
    { k: "risk_cluster_count", label: "风险簇", v: summary.risk_cluster_count ?? 0 },
    { k: "cross_scene_reuse_count", label: "跨场复用", v: summary.cross_scene_reuse_count ?? 0 },
  ];

  return (
    <>
      <div className="q-filters" role="group" aria-label="巡检范围">
        <label className="q-field">
          <span>文本层</span>
          <select className="select" value={filters.text_layer} onChange={(e) => setF("text_layer", e.target.value)}>
            {QUALITY_TEXT_LAYERS.map((o) => <option key={o.v} value={o.v}>{o.l}</option>)}
          </select>
        </label>
        <label className="q-field">
          <span>章</span>
          <select className="select" value={filters.chapter_id} onChange={(e) => setF("chapter_id", e.target.value)}>
            <option value="">全部章</option>
            {chapterOptions.map((c) => <option key={c.backendId} value={c.backendId}>{chapterLabel(c)}</option>)}
          </select>
        </label>
        <label className="q-field">
          <span>风险维度</span>
          <select className="select" value={filters.risk_type} onChange={(e) => setF("risk_type", e.target.value)}>
            <option value="">全部</option>
            {QUALITY_DIM_KEYS.map((k) => <option key={k} value={k}>{qDimLabel(k)}</option>)}
          </select>
        </label>
        <label className="q-field">
          <span>最低级别</span>
          <select className="select" value={filters.min_severity} onChange={(e) => setF("min_severity", e.target.value)}>
            <option value="">全部</option>
            {QUALITY_MIN_SEVERITIES.map((s) => <option key={s} value={s}>{qSevLabel(s)}</option>)}
          </select>
        </label>
        <button type="button" className="btn btn-accent btn-sm q-run" onClick={reload} disabled={st.loading}>
          {st.loading ? <Spinner size={13} /> : <I.Refresh size={13} />} {st.loading ? "巡检中…" : "重新巡检"}
        </button>
      </div>

      {overviewFailed && (
        <Notice tone="danger" title="巡检没有完成" className="q-notice"
          actions={<button type="button" className="btn btn-ghost btn-sm" onClick={reload} disabled={st.loading}>重试</button>}>
          {st.error}
        </Notice>
      )}

      {/* 巡检读不到时只留上面那一处报错（和成本看板一样）：不再同时摆一排 0 和一句「没有可巡检的稿件」——
          那是在说稿件是空的，而实际是没读到。上一轮巡检的结果还在时照常显示。 */}
      {!(overviewFailed && !st.overview) && (<>
        <div className="q-stats">
          {summaryCards.map((c) => <StatTile key={c.k} className="q-stat" label={c.label} value={c.v} tone={c.tone} />)}
        </div>

        {items.length === 0 ? (
          <EmptyState compact className="q-empty" title={st.loading ? "正在巡检…" : "没有可巡检的稿件"}>
            {st.loading ? null : "先在写作台写出正文（或在 AI 起草台起草），再回来巡检。"}
          </EmptyState>
        ) : (
          <div className="q-list">
            {items.map((it, i) => <QualityItem key={(it.object_id || "it") + ":" + i} item={it} chapters={chapters} go={go} />)}
          </div>
        )}
      </>)}

      {/* 临时文本扫描 */}
      <section className="q-scan" aria-labelledby="q-scan-title">
        <h2 className="q-section-title" id="q-scan-title">临时文本扫描</h2>
        <p className="q-section-desc">粘贴一段文字即时打分，不写入任何稿件。</p>
        <textarea className="textarea" rows={5}
          aria-label="要扫描的文字" placeholder="把要体检的段落贴进来…" value={draft} onChange={(e) => setDraft(e.target.value)} />
        <div className="q-scan-actions">
          <button type="button" className="btn btn-accent btn-sm" disabled={!draft.trim() || st.analyzing}
            onClick={() => qAnalyzeText(draft)}>
            {st.analyzing ? <Spinner size={13} /> : <I.Activity size={13} />} {st.analyzing ? "扫描中…" : "扫描这段文字"}
          </button>
        </div>
        {st.error && st.errorScope === "analyze" && (
          <Notice tone="danger" title="扫描没有完成" className="q-notice">{st.error}</Notice>
        )}
        {analyze && <AnalyzeResult data={analyze} />}
      </section>
    </>
  );
}

/* 一条发现：问题 / 改法是服务端给的中文（与写作台深改面板同一份）；onLocate 带着它的 signal_id
   去写作台——深改面板到了诊断就选中同一条并滚到那一句 */
function QualityFinding({ finding, evidence, onLocate }) {
  const text = qFindingText(finding);
  const excerpt = qPlainText(finding.context || evidence);
  const label = finding.label || qDimLabel(finding.dimension);
  const signalId = finding.signal_id || finding.quality_signal_id;
  return (
    <li className="q-finding">
      <div className="q-finding-head">
        <Tag tone={qSevTone(finding.severity)}>{qSevLabel(finding.severity)}</Tag>
        <strong title={QUALITY_DIMS[finding.dimension] || finding.label ? undefined : finding.dimension}>{label}</strong>
        {onLocate && signalId && (
          <button type="button" className="btn btn-quiet btn-sm q-finding-go" onClick={() => onLocate(signalId)}>
            <I.Pen size={12} /> 在写作台看这一处
          </button>
        )}
      </div>
      {text.issue && <p className="q-finding-issue" title={text.english || undefined}>{text.issue}</p>}
      {!text.issue && finding.issue && <p className="q-finding-issue">{finding.issue}</p>}
      {excerpt && <blockquote className="q-finding-evidence">{excerpt}</blockquote>}
      {text.fix && <p className="q-finding-fix">改法：{text.fix}</p>}
    </li>
  );
}

function QualityItem({ item, chapters, go }) {
  const [open, setOpen] = React.useState(false);
  const riskDims = qRiskDims(item);
  const findings = item.findings || [];
  const rna = item.recommended_next_action || {};
  const label = qObjectLabel(item, chapters);
  const layer = Q_ITEM_LAYER[item.text_layer] || "";
  const detailId = `q-item-${String(item.object_id || "x").replace(/[^a-zA-Z0-9_-]/g, "_")}-${item.text_layer || ""}`;
  /* 深链到这一场：写作台按场景 sid 定位（目录里的 sid 与后端 scene_id 不同名，靠目录换算），
     带着发现的 signal_id 进深改姿态——那边的诊断和这里是同一份，落地就是同一条；
     章级结果没有单一场景可去，就只回写作台。 */
  const sceneHit = item.object_type === "scene" ? findSceneByBackendId(chapters, item.scene_id || item.object_id) : null;
  const sceneSid = (sceneHit && sceneHit.scene.sid) || "";
  const toWriter = (signalId) => {
    if (!go) return;
    const posture = signalId ? { posture: "deep", signal_id: signalId } : "deep";
    go("writer", sceneSid
      ? [{ type: "ws:writer-scene", detail: sceneSid }, { type: "ws:writer-posture", detail: posture }]
      : []);
  };
  const topSignal = rna.signal_id || rna.quality_signal_id || null;
  const ignoredCount = Number(item.ignored_count) || 0;
  return (
    <article className={`q-item ${open ? "is-open" : ""}`}>
      {/* 详情只在展开时渲染：收起时不写 aria-controls，免得指向一个不存在的 id */}
      <button type="button" className="q-item-row" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-controls={open ? detailId : undefined}>
        <span className="q-item-main">
          <span className="q-item-title" title={item.source_ref || item.object_id}>{label}</span>
          <span className="q-item-meta">
            <Tag tone={item.score != null && item.score < 0.5 ? "warn" : "neutral"}>{qScore(item.score)}</Tag>
            {layer && <Tag tone="info" outline>{layer}</Tag>}
            {riskDims.slice(0, 5).map((k) => <Tag key={k} tone="accent" dot>{qDimLabel(k)}</Tag>)}
            {riskDims.length > 5 && <span className="q-more">另 {riskDims.length - 5} 项</span>}
            {ignoredCount > 0 && <Tag tone="neutral" outline title="在写作台深改面板里忽略过的发现，这里不再列出">已忽略 {ignoredCount}</Tag>}
          </span>
        </span>
        <span className="q-chev" data-open={open} aria-hidden="true"><I.ChevronDown size={16} /></span>
      </button>
      {open && (
        <div className="q-item-detail" id={detailId}>
          {findings.length === 0 ? (
            <p className="q-finding-issue">{ignoredCount > 0 ? "触发的发现都已在写作台忽略。" : "这一项没有触发风险维度。"}</p>
          ) : (
            <ul className="q-findings">
              {findings.map((f, i) => (
                <QualityFinding key={f.signal_id || i} finding={f} evidence={f.evidence_excerpt}
                  onLocate={sceneSid && go ? toWriter : null} />
              ))}
            </ul>
          )}
          {rna.action === "open_deepdesk_patch" && (
            <div className="q-item-actions">
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => toWriter(sceneSid ? topSignal : null)}>
                <I.Pen size={13} /> {sceneSid ? "去写作台处理这一场" : "去写作台处理"}
              </button>
            </div>
          )}
        </div>
      )}
    </article>
  );
}

function AnalyzeResult({ data }) {
  const spans = data.span_findings || [];
  const riskDims = qRiskDims(data);
  return (
    <div className="q-analyze card">
      <div className="q-item-meta">
        <Tag tone="info">总分 {qScore(data.score)}</Tag>
        {riskDims.map((k) => <Tag key={k} tone="accent" dot>{qDimLabel(k)}</Tag>)}
        {riskDims.length === 0 && <span className="q-more">没有触发风险维度。</span>}
      </div>
      {spans.length > 0 && (
        <ul className="q-findings">
          {spans.map((s, i) => <QualityFinding key={i} finding={s} evidence={s.evidence} />)}
        </ul>
      )}
    </div>
  );
}

function QualityChapterSet({ go }) {
  const st = useQualityState();
  const catalog = useCatalogChapters() || [];
  const [sel, setSel] = React.useState(() => new Set());
  const [terms, setTerms] = React.useState("");
  const [layer, setLayer] = React.useState("author_draft_preferred");

  // 章从目录来（后端 chapter_id = 目录卡的 backendId）：不必先巡检一轮，也不会只列出巡检碰巧命中的章
  const chapters = catalog.filter((c) => c && c.backendId);

  const toggle = (id) => setSel((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const run = () => qChapterSetReview({
    chapter_ids: [...sel],
    protected_terms: terms.split(/[,，\s]+/).map((t) => t.trim()).filter(Boolean),
    text_layer: layer,
  });

  const rv = st.review;
  const sm = (rv && rv.summary) || {};
  const scores = (rv && rv.scores) || {};
  const reviewed = (rv && rv.chapters) || [];
  const scenes = (rv && rv.scenes) || [];
  const repeated = (rv && rv.repeated_patterns) || [];
  const safety = (rv && rv.reference_safety_findings) || [];

  return (
    <div className="q-set">
      <p className="q-section-desc">选几章一起体检：找跨章重复的写法、回收 / 铺垫缺口，以及受保护词有没有漏进正文。</p>
      {chapters.length === 0 ? (
        <EmptyState compact className="q-empty" title="目录里还没有章">先在构思或章节编排里建章，再回来复审。</EmptyState>
      ) : (
        <div className="q-set-form card">
          <fieldset className="q-set-chapters">
            <legend className="ws-sr-only">选择要复审的章</legend>
            {chapters.map((c) => (
              <label key={c.backendId} className={`q-set-chip ${sel.has(c.backendId) ? "is-on" : ""}`}>
                <input type="checkbox" checked={sel.has(c.backendId)} onChange={() => toggle(c.backendId)} />
                <span>{chapterLabel(c)}</span>
              </label>
            ))}
          </fieldset>
          <div className="q-filters">
            <label className="q-field q-field-grow">
              <span>受保护词</span>
              <input className="input" placeholder="逗号分隔，可空" value={terms} onChange={(e) => setTerms(e.target.value)} />
            </label>
            <label className="q-field">
              <span>文本层</span>
              <select className="select" value={layer} onChange={(e) => setLayer(e.target.value)}>
                {QUALITY_TEXT_LAYERS.map((o) => <option key={o.v} value={o.v}>{o.l}</option>)}
              </select>
            </label>
            <button type="button" className="btn btn-accent btn-sm q-run" disabled={sel.size === 0 || st.reviewing} onClick={run}>
              {st.reviewing ? <Spinner size={13} /> : <I.ShieldCheck size={13} />} {st.reviewing ? "复审中…" : `复审选中的 ${sel.size} 章`}
            </button>
          </div>
        </div>
      )}

      {st.error && st.errorScope === "review" && (
        <Notice tone="danger" title="章组复审没有完成" className="q-notice">{st.error}</Notice>
      )}

      {rv && (
        <div className="q-set-result">
          <div className="q-stats">
            {[
              { label: "复审章数", v: sm.chapter_count ?? 0 },
              { label: "场景数", v: sm.scene_count ?? 0 },
              { label: "平均分", v: sm.mean_score == null ? "—" : qPct(sm.mean_score) },
              { label: "高风险项", v: sm.high_risk_count ?? 0 },
              { label: "重复模式", v: sm.repeated_pattern_count ?? 0 },
              { label: "受保护词命中", v: sm.reference_safety_finding_count ?? 0, tone: sm.reference_safety_finding_count ? "danger" : undefined },
            ].map((c) => <StatTile key={c.label} className="q-stat" label={c.label} value={c.v} tone={c.tone} />)}
          </div>
          <div className="q-item-meta">
            <Tag tone="info">文学质量 {qScore(scores.literary_quality)}</Tag>
            <Tag tone="info">跨章弧光 {qScore(scores.cross_chapter_arc)}</Tag>
            <Tag tone="info">参考安全 {qScore(scores.reference_safety)}</Tag>
          </div>
          {repeated.length > 0 && (
            <section className="q-block">
              <h3 className="q-block-title">跨章重复模式 <span className="q-more">{repeated.length}</span></h3>
              <ul className="q-lines">
                {repeated.slice(0, 12).map((r, i) => (
                  <li key={i}>
                    「{r.token}」× {r.count}
                    <span className="q-more">{(r.chapter_ids || []).map((id) => chapterLabelById(catalog, id, { withTitle: false })).join("、")}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {safety.length > 0 && (
            <section className="q-block is-danger">
              <h3 className="q-block-title">受保护词命中 <span className="q-more">{safety.length}</span></h3>
              <ul className="q-lines">
                {safety.slice(0, 12).map((f, i) => (
                  <li key={i}>{f.term ? `「${f.term}」` : ""}{qPlainText(f.evidence_excerpt) || f.issue || ""}</li>
                ))}
              </ul>
            </section>
          )}
          {(reviewed.length > 0 || scenes.length > 0) && (
            <div className="q-list">
              {[...reviewed, ...scenes].map((it, i) => <QualityItem key={(it.object_id || "it") + ":" + i} item={it} chapters={catalog} go={go} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* store 与标签表原先就从这里导出（单测这样 import），拆文件后照样从这里拿得到 */
export { WsQuality, qLoadOverview, qAnalyzeText, qChapterSetReview, qSnapshot, useQualityState, QUALITY_DIMS, QUALITY_DIM_KEYS, QUALITY_SEV, QUALITY_TEXT_LAYERS };
