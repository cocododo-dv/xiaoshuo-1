import React from "react";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, Spinner, Tag } from "./ws-ui.jsx";
import { STYLE_LAYER_LABELS, STYLE_LAYER_ORDER, paragraphTypeLabel } from "./ws-labels.js";
import {
  SR_ACTIVITY_WHERE, srActivityKindLabel, srActivityView, srClassifyEstimateText, srCloudPolicyMeta, srFormatPct,
  srFormatWhen, srJobErrorText, srProvenanceView, srRetypeUnfinished,
} from "./ws-styleref-model.js";
import {
  srActivityFor, srBookDetail, srLoadBookDetail, srLoadClassifyEstimate, srLoadRuntime, srResumeClassification, srRetype,
  srRuntime,
} from "./ws-styleref-store.js";
import { SrErrorLine, SrProgressBar, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 第一步「参考书」：段落分类（进度 / 继续 / 来源与一致率 / 用模型重新分类）、这本书的事实、
   段落类型分布、语料够不够。
   旧版导入的书大部分段落是启发式规则标的（一致率往往不到一半），挑学习样本与样例窗都看段落类型：这里明确提醒，
   给「用模型重新分类（保留画像）」——先报费用（调用 / token / 分钟）再确认；分好后建议重新学习文风。
   ========================================================== */

const SR_INPUT_LABEL = { skip: "语料不足", low: "偏少", medium: "适中", high: "充足" };
const SR_INPUT_TONE = { skip: "neutral", low: "info", medium: "warn", high: "ok" };

const srIsCancelCode = (code) => code === "STYLE_REFERENCE_JOB_CANCELLED";

function srParaDist(stats) {
  const dist = (stats && stats.paragraph_type_distribution) || {};
  return Object.entries(dist)
    .map(([key, v]) => ({ key, label: paragraphTypeLabel(key), v: Number(v) || 0 }))
    .sort((a, b) => b.v - a.v);
}

export function SrOverview({ book, onAction }) {
  useSrStore("detail", "activity", "books");
  React.useEffect(() => { srLoadBookDetail(book.id); }, [book.id]);
  const detail = srBookDetail(book.id);
  const stats = (detail && detail.data && detail.data.stats_json) || null;
  const classifyPending = !!(book.rawStatus && book.rawStatus !== "ready");
  return (
    <div className="sr-overview">
      <SrClassifyCard book={book} onAction={onAction} />
      <div className="sr-ov-grid">
        <SrBookFactsCard book={book} detail={detail && detail.data} />
        <SrInputCard stats={stats} />
      </div>
      <SrDistCard stats={stats} classifyPending={classifyPending} loaded={!!(detail && detail.phase !== "loading")} />
    </div>
  );
}

/* 段落分类：在跑 → 进度；没分完 →「继续分类」；分完 → 来源与一致率（旧版启发式要提醒）+「用模型重新分类」；
   「用模型重新分类」（就地重标，书一直是 ready）失败 / 取消了 → 说清楚并给「继续分类」（只补剩下的，不用整本重付） */
function SrClassifyCard({ book, onAction }) {
  const [busy, setBusy] = React.useState(null);
  const [error, setError] = React.useState(null);
  const running = srActivityFor(book.id, "classify");
  const view = running ? srActivityView(running) : null;
  const incomplete = !!(book.rawStatus && book.rawStatus !== "ready" && !running);
  const retypeLeft = !running ? srRetypeUnfinished(book) : null;
  const learning = !!srActivityFor(book.id, "learn");
  const provenance = srProvenanceView(book.provenance);
  const jobError = book.classification && book.classification.error;
  React.useEffect(() => { srLoadRuntime(); }, []);
  /* 分类要用模型（GET /runtime 说的是两个分类节点的实际路由）；「仅本机模型」的书还要求分类节点在本机 */
  const runtime = srRuntime();
  const rt = runtime.phase === "ready" ? runtime.data : null;
  const modelGate = !rt ? null
    : rt.llm_enabled === false ? "还没有接入模型：重新分类要由模型给每一段分类。"
    : book.cloudPolicy === "local_only" && rt.llm_is_local === false ? "这本书设为「仅本机模型」，但分类用的模型不在本机：先在设置里把段落分类换成本机模型。"
    : null;

  const resume = async () => {
    if (busy) return;
    setBusy("resume"); setError(null);
    try { await srResumeClassification(book.id); }
    catch (e) { setError(e); }
    finally { setBusy(null); }
  };

  const retype = async () => {
    if (busy) return;
    setBusy("estimate"); setError(null);
    let estimate = null;
    try { estimate = await srLoadClassifyEstimate(book.id, { force: true }); }
    catch (e) { estimate = null; }
    setBusy(null);
    const cost = srClassifyEstimateText(estimate);
    const ok = await wsConfirm({
      title: `用模型重新分类《${book.title}》？`,
      body: `${cost ? `${cost}。` : "读不到费用估算。"}正文不变，文风画像和用在作品上的设置都保留；分好之后建议重新学习文风（挑样本、打标签都看段落类型）。`,
      confirmLabel: "开始重新分类",
    });
    if (!ok) return;
    setBusy("retype");
    try { await srRetype(book.id); }
    catch (e) { setError(e); }
    finally { setBusy(null); }
  };

  const pill = running ? { tone: "warn", label: `分类中 ${view.percentText}` }
    : incomplete ? { tone: "danger", label: "未完成" }
    : retypeLeft ? { tone: "warn", label: retypeLeft.cancelled ? "重新分类已取消" : "重新分类没完成" }
    : provenance.legacy ? { tone: "warn", label: "旧版规则标的" }
    : provenance.kind === "llm" ? { tone: "ok", label: "模型已分好" }
    : { tone: "neutral", label: "已分好" };

  return (
    <div className="card" data-testid="sr-overview-classify">
      <div className="card-head">
        <div>
          <div className="card-title">段落分类</div>
          <div className="card-sub">每一段标上类型（叙述、对话、心理……）。学习时挑样本、给片段打标签、起草时挑样例窗都要看它。</div>
        </div>
        <Tag tone={pill.tone} dot>{pill.label}</Tag>
      </div>
      {running ? (
        <div className="sr-ov-live">
          <SrProgressBar percent={view.percent} label="段落分类进度" />
          <div className="sr-activity-meta">{srActivityKindLabel(running)} · {view.detail}</div>
          <p className="sr-ov-hint">{`可以在${SR_ACTIVITY_WHERE}里取消，之后还能从断点接着分。`}</p>
        </div>
      ) : incomplete ? (
        <div className="sr-ov-live">
          <p className="sr-ov-text">
            {jobError && srIsCancelCode(jobError.code) ? "分类被取消了。" : "分类没有完成。"}
            {book.classification && book.classification.batches_total
              ? ` 已分好 ${book.classification.batches_done}/${book.classification.batches_total} 批，继续分类只补剩下的。`
              : ""}
          </p>
          {jobError && !srIsCancelCode(jobError.code) && (
            <p className="sr-ov-hint" data-testid="sr-overview-classify-reason">原因：{srJobErrorText(jobError, { resumable: true })}</p>
          )}
          {modelGate && <p className="sr-ov-hint" data-testid="sr-overview-model-gate">{modelGate}</p>}
          {modelGate && onAction && (
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => onAction({ type: "settings" })}>去设置模型</button>
          )}
          <button type="button" className="btn btn-accent btn-sm" data-testid="sr-overview-resume" disabled={!!busy || !!modelGate} onClick={resume}>
            {busy === "resume" ? <><Spinner size={12} /> 启动中…</> : "继续分类"}
          </button>
        </div>
      ) : (
        <>
          {retypeLeft ? (
            <div className="sr-ov-live" data-testid="sr-overview-retype-unfinished">
              <p className="sr-ov-text">
                {retypeLeft.cancelled ? "上次「用模型重新分类」被取消了" : "上次「用模型重新分类」没有做完"}
                {retypeLeft.batchesTotal ? `：已分好 ${retypeLeft.batchesDone}/${retypeLeft.batchesTotal} 批，继续分类只补剩下的` : ""}。
                正文、文风画像和用在作品上的设置都没动；还没重新分到的段落暂时仍是原来的类型。
              </p>
              {retypeLeft.error && !retypeLeft.cancelled && (
                <p className="sr-ov-hint" data-testid="sr-overview-classify-reason">原因：{srJobErrorText(retypeLeft.error, { resumable: true })}</p>
              )}
            </div>
          ) : provenance.legacy ? (
            <Notice tone="warn" testId="sr-overview-legacy-types" title="段落类型大多是旧版规则标的">
              {provenance.kind === "legacy_heuristic"
                ? `这本书有 ${provenance.heuristicParagraphs.toLocaleString()} 段是旧版导入时用启发式规则标的类型${provenance.agreement != null ? `，在抽查的段落上和模型只有 ${srFormatPct(provenance.agreement)} 一致` : ""}。`
                : "这本书的段落类型全部是启发式规则标的。"}
              挑学习样本和样例窗都看段落类型，建议用模型重新分类：正文、文风画像和用在作品上的设置都保留。
            </Notice>
          ) : (
            <dl className="sr-calib">
              {/* 没有来源记录（很早导入、导入信息缺失）时如实说没有记录，不冒充「模型分好的」 */}
              <div><dt>分类方式</dt><dd>{provenance.kind === "llm" ? "模型逐批分类" : "没有记录"}</dd></div>
              <div><dt>模型分好的段数</dt><dd className="tab-num">{provenance.llmParagraphs ? provenance.llmParagraphs.toLocaleString() : "—"}</dd></div>
              {provenance.kind === "llm" && (
                <div><dt>快慢模型一致率</dt><dd className="tab-num">{provenance.agreement != null ? srFormatPct(provenance.agreement) : "同一个模型，未对照"}</dd></div>
              )}
              <div><dt>类型版本</dt><dd className="tab-num">第 {book.typesRevision || 0} 版</dd></div>
            </dl>
          )}
          <div className="sr-ov-foot">
            <span className="sr-ov-hint" data-testid={modelGate ? "sr-overview-model-gate" : undefined}>
              {modelGate
                || (retypeLeft && retypeLeft.resumable
                  ? "继续分类从断点接着分，已经分好的批不再花模型调用；从头重新分类会整本重新计费。"
                  : "重新分类不动正文，文风画像与用在作品上的设置都保留；会先告诉你大约要多少次模型调用。")}
            </span>
            {modelGate && onAction && (
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => onAction({ type: "settings" })}>去设置模型</button>
            )}
            {retypeLeft && retypeLeft.resumable && (
              <button
                type="button"
                className="btn btn-accent btn-sm"
                data-testid="sr-overview-resume"
                disabled={!!busy || !!modelGate || learning}
                title={learning ? "正在学习文风，学完再继续分类" : undefined}
                onClick={resume}
              >
                {busy === "resume" ? <><Spinner size={12} /> 启动中…</> : "继续分类"}
              </button>
            )}
            <button
              type="button"
              className={`btn ${provenance.legacy && !retypeLeft ? "btn-accent" : "btn-ghost"} btn-sm`}
              data-testid="sr-overview-retype"
              disabled={!!busy || !!modelGate || book.rawStatus !== "ready" || learning}
              title={learning ? "正在学习文风，学完再重新分类" : undefined}
              onClick={retype}
            >
              {busy === "estimate" ? <><Spinner size={12} /> 估算中…</>
                : busy === "retype" ? <><Spinner size={12} /> 启动中…</>
                : retypeLeft ? "从头重新分类" : "用模型重新分类（保留画像）"}
            </button>
          </div>
        </>
      )}
      <SrErrorLine error={error} onAction={onAction} testId="sr-overview-error" />
    </div>
  );
}

function SrBookFactsCard({ book, detail }) {
  const policy = srCloudPolicyMeta(book.cloudPolicy);
  const paragraphs = book.paragraphCount || (detail && detail.paragraph_count) || null;
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">这本书</div></div></div>
      <dl className="sr-calib">
        <div><dt>字数</dt><dd className="tab-num">{book.chars.toLocaleString()}</dd></div>
        <div><dt>段数</dt><dd className="tab-num">{paragraphs ? Number(paragraphs).toLocaleString() : "—"}</dd></div>
        <div className="is-wide"><dt>原文能发到哪里</dt><dd>{policy ? policy.label : "—"}</dd></div>
        {policy && <div className="is-wide"><dt>这意味着</dt><dd className="sr-calib-note">{policy.detail}</dd></div>}
        <div><dt>导入于</dt><dd>{srFormatWhen(book.createdAt) || "—"}</dd></div>
      </dl>
    </div>
  );
}

/* 语料够不够：每一层单独评估，不足的层学习时跳过；没有评估数据如实写「未评估」 */
function SrInputCard({ stats }) {
  const assessed = (stats && stats.input_assessment) || null;
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">语料够不够</div><div className="card-sub">四层分别评估，语料不足的层学习时跳过</div></div></div>
      <div className="sr-input-list">
        {STYLE_LAYER_ORDER.map((layer) => {
          const level = (assessed && assessed[layer]) || null;
          return (
            <div key={layer} className="sr-input-row">
              <span className="sr-input-name">{STYLE_LAYER_LABELS[layer]}层</span>
              {level ? <Tag tone={SR_INPUT_TONE[level] || "neutral"}>{SR_INPUT_LABEL[level] || level}</Tag> : <span className="sr-input-unknown">未评估</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SrDistCard({ stats, classifyPending, loaded }) {
  const dist = stats ? srParaDist(stats) : [];
  const max = Math.max(...dist.map((d) => d.v), 0.01);
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">段落类型分布</div><div className="card-sub">全书实测</div></div></div>
      <div className="sr-dist">
        {dist.map((d) => (
          <div key={d.key} className="sr-dist-row">
            <span className="sr-dist-label">{d.label}</span>
            <div className="sr-dist-bar"><div className="sr-dist-fill" style={{ width: `${(d.v / max) * 100}%` }} /></div>
            <span className="sr-dist-val tab-num">{srFormatPct(d.v)}</span>
          </div>
        ))}
        {dist.length === 0 && (
          <p className="sr-ov-text sr-ov-muted">{!loaded ? "正在读取…" : classifyPending ? "段落分类完成后显示各类段落的占比。" : "这本书没有段落类型分布。"}</p>
        )}
      </div>
    </div>
  );
}
