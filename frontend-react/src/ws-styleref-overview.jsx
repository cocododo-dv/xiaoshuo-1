import React from "react";
import { Spinner, Tag } from "./ws-ui.jsx";
import { SR_ACTIVITY_WHERE, SR_LAYERS, SR_PARA_LABEL, srFormatPct, srMetricRows } from "./ws-styleref-model.js";
import { srActivityFor, srResumeClassification } from "./ws-styleref-store.js";
import { srActivityView } from "./ws-styleref-activity.jsx";
import { SrProgressBar, srNotify, useSrDeep, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 概览：段落分类、抽取进展、统计基线、语料够不够、段落类型分布
   ========================================================== */

const SR_INPUT_LABEL = { skip: "语料不足", low: "偏少", medium: "适中", high: "充足" };
const SR_INPUT_TONE = { skip: "neutral", low: "info", medium: "warn", high: "ok" };

/* 锚定集之外的段落是怎么分类的（2026-09-15 严格 LLM：每一段都是 LLM 分的，余段按锚定校准
   结果走快模型或强模型；书不超过锚定集时没有余段）。老书若还有启发式份额，如实标出。 */
export function srRestClassifierLabel(calib) {
  const heuristicCount = Number(calib.heuristic_classified_paragraphs || 0);
  if (calib.rest_classifier === "heuristic") {
    return `${heuristicCount.toLocaleString()} 段启发式（旧版导入；重新分类可全部交给模型）`;
  }
  if (calib.rest_classifier === "strong_llm") return "强模型逐批";
  if (calib.rest_classifier === "fast_llm") return "快模型逐批";
  if (!calib.rest_classifier) return "无余段（整本在锚定集内）";
  return String(calib.rest_classifier);
}

/* stats_json.paragraph_type_distribution → [{type,key,v}]（降序） */
function srParaDist(stats) {
  const dist = (stats && stats.paragraph_type_distribution) || {};
  return Object.entries(dist)
    .map(([key, v]) => ({ type: SR_PARA_LABEL[key] || key, key, v: Number(v) || 0 }))
    .sort((a, b) => b.v - a.v);
}

/* reclassifyLock：页头判断出的「现在不能重新分类」的原因（在抽取、上一个操作没完…）；
   有原因时按钮置灰并把原因写在旁边，不再点了没反应。 */
export function SrOverview({ book, onReclassify, reclassifyLock = null }) {
  const deep = useSrDeep(book);
  /* 正在跑的抽取（活动表）优先于缓存里的上一条 run：srPickLatestRun 偏好 done，重跑期间
     不能让总览写着「抽取完成」。 */
  useSrStore("activity");
  const stats = (deep && deep.book && deep.book.stats_json) || null;
  const classifyPending = !!(book.rawStatus && book.rawStatus !== "ready");

  return (
    <div className="sr-overview">
      <div className="sr-ov-grid">
        <SrClassifyCard book={book} stats={stats} onReclassify={onReclassify} reclassifyLock={reclassifyLock} />
        <SrExtractCard book={book} deep={deep} />
      </div>

      <div className="sr-ov-grid sr-ov-grid-wide">
        <SrBaselineCard stats={stats} loaded={!!(deep && deep.loaded)} classifyPending={classifyPending} />
        <SrInputCard stats={stats} />
      </div>

      <SrDistCard stats={stats} classifyPending={classifyPending} />
    </div>
  );
}

/* 段落分类（2026-09-15 严格 LLM 的后台任务）：在跑时显示进度，没完成时给「继续分类」，完成后给校准结果。 */
function SrClassifyCard({ book, stats, onReclassify, reclassifyLock }) {
  const [resumeBusy, setResumeBusy] = React.useState(false);
  const onResume = async () => {
    if (resumeBusy) return;
    setResumeBusy(true);
    try { await srResumeClassification(book.id); }
    catch (e) { srNotify("继续分类失败：" + ((e && e.message) || e)); }
    finally { setResumeBusy(false); }
  };
  const classifying = srActivityFor(book.id, "import") || srActivityFor(book.id, "reclassify");
  const view = classifying ? srActivityView(classifying) : null;
  const rawStatus = book.rawStatus || null;
  const incomplete = !!(rawStatus && rawStatus !== "ready" && !classifying);
  const calib = (stats && stats.classifier_calibration) || null;
  const error = book.classification && book.classification.error;
  const pill = classifying ? { tone: "warn", label: `分类中 ${view.percentText}` }
    : rawStatus === "cancelling" ? { tone: "neutral", label: "取消中" }
    : incomplete ? { tone: "danger", label: "未完成" }
    : { tone: "ok", label: "已完成" };
  return (
    <div className="card" data-testid="sr-overview-classify">
      <div className="card-head">
        <div><div className="card-title">段落分类</div><div className="card-sub">整本由模型逐批给段落分类型，完成后才能抽取</div></div>
        <Tag tone={pill.tone} dot>{pill.label}</Tag>
      </div>
      {classifying ? (
        <div className="sr-ov-live">
          <SrProgressBar percent={view.percent} label="段落分类进度" />
          <div className="sr-import-meta">{view.detail}</div>
          <p className="sr-ov-hint">{`可在${SR_ACTIVITY_WHERE}里取消，之后还能从断点继续。`}</p>
        </div>
      ) : incomplete ? (
        <div className="sr-ov-live">
          <p className="sr-ov-text">
            {(error && (error.code === "STYLE_REFERENCE_JOB_CANCELLED" || error.code === "STYLE_REFERENCE_IMPORT_CANCELLED"))
              ? "分类被取消。"
              : `分类没有完成${error && error.message ? `：${error.message}` : "。"}`}
            {book.classification && book.classification.batches_total ? ` 已完成 ${book.classification.batches_done}/${book.classification.batches_total} 批，继续分类只补剩下的。` : ""}
          </p>
          <button type="button" className="btn btn-accent btn-sm" data-testid="sr-overview-resume" disabled={resumeBusy} onClick={onResume}>
            {resumeBusy ? <><Spinner size={12} /> 启动中…</> : "继续分类"}
          </button>
        </div>
      ) : (
        <>
          <dl className="sr-calib">
            <div><dt>锚定集</dt><dd>{calib && calib.anchor_size ? `前 ${Number(calib.anchor_size).toLocaleString()} 段，强模型` : "—"}</dd></div>
            <div><dt>快模型一致率</dt><dd className="tab-num">{calib && calib.fast_model_agreement != null ? srFormatPct(calib.fast_model_agreement) : "—"}</dd></div>
            <div><dt>余段改走强模型</dt><dd>{calib && calib.fallback_to_strong != null ? (calib.fallback_to_strong ? "是" : "否") : "—"}</dd></div>
            <div><dt>模型分类段数</dt><dd className="tab-num">{calib && calib.llm_classified_paragraphs != null ? Number(calib.llm_classified_paragraphs).toLocaleString() : "—"}</dd></div>
            <div className="is-wide"><dt>锚定集之外</dt><dd>{calib ? srRestClassifierLabel(calib) : "—"}</dd></div>
          </dl>
          {onReclassify && (
            <div className="sr-ov-foot">
              <span className="sr-ov-hint" data-testid="sr-overview-reclassify-hint">
                {reclassifyLock ? `${reclassifyLock}。` : "重新分类会先删掉这本书的抽取结果、画像和应用绑定。"}
              </span>
              <button type="button" className="btn btn-danger btn-sm" data-testid="sr-overview-reclassify" disabled={!!reclassifyLock} onClick={onReclassify}>重新分类…</button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function SrExtractCard({ book, deep }) {
  const live = srActivityFor(book.id, "extract");
  const view = live ? srActivityView(live) : null;
  const run = deep && deep.run;
  const runStatus = run ? run.status : null;
  const progress = (run && run.coverage_json && run.coverage_json.progress) || null;
  const dimCovered = deep && deep.dimCounts ? Object.keys(deep.dimCounts).length : 0;
  const pill = live ? { tone: "warn", label: `抽取中 ${view.percentText}` }
    : runStatus === "done" ? { tone: "ok", label: "抽取完成" }
    : runStatus === "running" || runStatus === "queued" ? { tone: "warn", label: "抽取中" }
    : runStatus === "failed" ? { tone: "danger", label: "抽取失败" }
    : runStatus === "cancelled" ? { tone: "neutral", label: "已取消" }
    : { tone: "neutral", label: "尚未抽取" };
  return (
    <div className="card">
      <div className="card-head">
        <div><div className="card-title">抽取进展</div><div className="card-sub">{live ? "正在后台抽取" : "最近一次抽取"}</div></div>
        <Tag tone={pill.tone} dot>{pill.label}</Tag>
      </div>
      {live ? (
        <div className="sr-ov-live" data-testid="sr-overview-live">
          <SrProgressBar percent={view.percent} label="后台抽取进度" />
          <div className="sr-import-meta">{view.detail}</div>
          <p className="sr-ov-hint">{`可在${SR_ACTIVITY_WHERE}里取消；完成后维度矩阵自动刷新。`}</p>
        </div>
      ) : runStatus ? (
        <dl className="sr-calib">
          <div><dt>覆盖维度</dt><dd className="tab-num">{dimCovered} / 16</dd></div>
          {progress && <div><dt>分析层</dt><dd className="tab-num">{progress.layers_done ?? 0} / {progress.layers_total ?? 4}</dd></div>}
        </dl>
      ) : (
        <p className="sr-ov-text sr-ov-muted">还没有抽取记录。点页头「开始抽取」启动后台抽取（需已接入模型），完成后「维度矩阵」显示这本书的真实观察。</p>
      )}
    </div>
  );
}

function SrBaselineCard({ stats, loaded, classifyPending }) {
  const metrics = stats ? srMetricRows(stats.metrics) : [];
  return (
    <div className="card sr-ov-metrics">
      <div className="card-head">
        <div><div className="card-title">统计基线</div><div className="card-sub">全书计算，用于校准与回测，不是生成时的配额</div></div>
      </div>
      {metrics.length > 0 ? (
        <div className="sr-metric-grid">
          {metrics.map((m) => (
            <div key={m.key} className="sr-metric">
              <div className="sr-metric-name">{m.name}</div>
              <div className="sr-metric-val tab-num">{m.value}{m.unit && <span className="sr-metric-unit"> {m.unit}</span>}</div>
              {m.spread && <div className="sr-metric-std tab-num">波动 {m.spread}</div>}
            </div>
          ))}
        </div>
      ) : (
        <p className="sr-ov-text sr-ov-muted">{!loaded ? "正在读取统计基线…" : classifyPending ? "段落分类完成后计算统计基线。" : "这本书没有统计基线。"}</p>
      )}
    </div>
  );
}

/* 没有评估数据就如实写「未评估」，不一律显示「偏少」 */
function SrInputCard({ stats }) {
  const assessed = (stats && stats.input_assessment) || null;
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">语料够不够</div><div className="card-sub">每个分析层单独评估，不足的层抽取时会跳过</div></div></div>
      <div className="sr-input-list">
        {SR_LAYERS.map((l) => {
          const level = (assessed && assessed[l.id]) || null;
          return (
            <div key={l.id} className="sr-input-row">
              <span className="sr-input-name">{l.name}</span>
              {level
                ? <Tag tone={SR_INPUT_TONE[level] || "neutral"}>{SR_INPUT_LABEL[level] || level}</Tag>
                : <span className="sr-input-unknown">未评估</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SrDistCard({ stats, classifyPending }) {
  const dist = stats ? srParaDist(stats) : [];
  const max = Math.max(...dist.map((d) => d.v), 0.01);
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">段落类型分布</div><div className="card-sub">8 类，本书实测</div></div></div>
      <div className="sr-dist">
        {dist.map((d) => (
          <div key={d.key} className="sr-dist-row">
            <span className="sr-dist-label">{d.type}</span>
            <div className="sr-dist-bar">
              <div className="sr-dist-fill" style={{ width: (d.v / max * 100) + "%" }} />
            </div>
            <span className="sr-dist-val tab-num">{srFormatPct(d.v)}</span>
          </div>
        ))}
        {dist.length === 0 && (
          <p className="sr-ov-text sr-ov-muted">{classifyPending ? "段落分类完成后显示各类段落的占比。" : "这本书没有段落类型分布。"}</p>
        )}
      </div>
    </div>
  );
}
