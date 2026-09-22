import React from "react";
import { I } from "./icons.jsx";
import { EmptyState, Notice, SectionLabel, Segmented, Spinner, Tag } from "./ws-ui.jsx";
import { srFormatDuration, srFormatPct, srFormatWhen, srMetricMeta } from "./ws-styleref-model.js";
import { srLoadDeep, srLoadReport, srValidate } from "./ws-styleref-store.js";
import { useSrDeep } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 回测校验 stage + ValidationReportCard
   四路校验：量化对齐（自适应容差）/ 语义评分（模型评审，雷达图）/ 抄袭检测（逐字比对）/ 禁忌检查。
   有画像 → 快速（sync_only）内联出结果 / 完整（async_full）轮询报告；无画像 → 空态引导。
   本次会话还没跑过时显示这份画像最近一次有结论的回测（「上次回测」，来自深层数据的 lastReport），
   和步骤条的「回测校验 已完成」说同一件事。
   请求在 ws-styleref-store.js，指标名与总览、画像基线共用 ws-styleref-model.js 的 SR_METRIC_META。
   ========================================================== */

/* ---- 真实 report 字段映射 ---- */
const SRV_SEMANTIC_AXIS = {
  language: "语言贴合", narrative: "叙事贴合", scene: "场景贴合", theme: "主题贴合",
  coherence: "连贯性", originality: "原创度", emotion: "情感基调", style: "风格贴合",
};

/* 后端 ValidationReport / report → 组件统一形状；缺数据返 null */
function srvNormalize(rep) {
  if (!rep) return null;
  const quant = (rep.quantitative_json || []).map(q => {
    const meta = srMetricMeta(q.metric);
    return {
      name: meta.name, pct: !!meta.pct, unit: meta.unit || "",
      target: q.target_mean, std: q.target_std, actual: q.actual,
      tolerance: q.tolerance, passed: q.passed, deviation: q.deviation_ratio,
    };
  });
  const semantic = (rep.semantic_json || []).map(s => ({
    axis: SRV_SEMANTIC_AXIS[s.dimension] || s.dimension,
    v: Math.max(0, Math.min(1, (Number(s.score) || 0) / 10)),
    score: Number(s.score) || 0,
    explanation: s.explanation || "",
  }));
  const plag = rep.plagiarism_json || {};
  const hits = plag.hits || [];
  const maxRun = hits.reduce((m, h) => Math.max(m, h.matched_length || 0), 0);
  const plagiarism = {
    passed: plag.passed !== false,
    ngram: plag.ngram_size || 8,
    threshold: plag.threshold_chars || 12,
    maxRun,
    flags: hits.map(h => ({ run: h.matched_length, text: h.matched_text, source: "与参考语料重叠", level: "hit" })),
  };
  const forbidden = (rep.forbidden_hits_json || []).map(f => ({
    statement: f.pattern_statement, triggered: true, excerpt: f.matched_excerpt,
    severity: f.severity, note: f.severity === "error" ? "硬性禁忌触发" : "",
  }));
  return {
    verdict: rep.verdict, mode_executed: rep.mode_executed,
    quant, semantic, plagiarism, forbidden,
    semanticPending: rep.mode_executed === "sync_only" && semantic.length === 0,
  };
}

/* 前端只在服务端彻底失联时才放弃轮询（报告行有 10 分钟孤儿回收，正常失败会先变 failed）。 */
const SRV_POLL_HARD_CAP_MS = 30 * 60 * 1000;

/* 纯函数：running 报告的原始快照 → 四路的完成状态。后端 worker 先把量化 / 抄袭 / 本地禁忌落库
   （plagiarism_json.passed 有值即本地三路完成），再跑语义路（semantic_json 有值即完成）；
   禁忌语义判定只在报告终态才落，所以 async_full 下禁忌一行要等 verdict。 */
function srvRunningRows(partial, mode) {
  const plag = partial && partial.plagiarism_json;
  const localDone = !!(plag && typeof plag === "object" && plag.passed != null);
  const semanticDone = !!(partial && Array.isArray(partial.semantic_json) && partial.semantic_json.length);
  const finished = !!(partial && partial.verdict);
  const rows = [
    { id: "quant", label: "量化对齐 · 本地计算", done: localDone },
  ];
  if (mode === "async_full") rows.push({ id: "semantic", label: "语义评分 · 模型评审", done: semanticDone || finished });
  rows.push({ id: "plagiarism", label: "抄袭检测 · 逐字比对", done: localDone });
  rows.push({ id: "forbidden", label: "禁忌检查 · 逐条判定", done: mode === "async_full" ? finished : localDone });
  return rows;
}

function srvVerdictMeta(v) {
  switch (v) {
    case "pass": return { kind: "pass", label: "通过", sub: "各路校验都达标" };
    case "plagiarism": return { kind: "plagiarism", label: "疑似抄袭", sub: "与原文的最长重叠超过阈值" };
    case "fail": return { kind: "fail", label: "未通过", sub: "触发了硬性禁忌，或几路都不达标" };
    case "partial": return { kind: "partial", label: "部分通过", sub: "建议带着修改意见再写一轮" };
    default: return { kind: "idle", label: "结论不明", sub: "" };
  }
}

export { srvRunningRows, SRV_POLL_HARD_CAP_MS };

export function SrValidation({ book, go }) {
  const deep = useSrDeep(book);
  const profileId = deep && deep.profileId;
  const realMode = !!profileId;
  const profileTitle = (deep && deep.profile && deep.profile.title) || (book ? `《${book.title}》风格画像` : "这份画像");
  const textId = React.useId();
  const hintId = React.useId();

  const [mode, setMode] = React.useState("async_full");
  const [text, setText] = React.useState("");
  const [running, setRunning] = React.useState(false);
  const [report, setReport] = React.useState(null);   // 归一化的真实报告
  const [partial, setPartial] = React.useState(null); // running 报告的原始快照（本地三路先落库，逐路点亮）
  const [runStartedAt, setRunStartedAt] = React.useState(null);
  const [, tick] = React.useState(0);
  const [done, setDone] = React.useState(false);
  const [err, setErr] = React.useState(null);
  // 错误代码只给排查用：放进报错的悬停提示，不拼进作者读的那句话
  const [errCode, setErrCode] = React.useState("");
  const pollRef = React.useRef(null);
  const pollGeneration = React.useRef(0);
  React.useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);
  React.useEffect(() => {
    pollGeneration.current += 1;
    clearTimeout(pollRef.current);
    return () => {
      pollGeneration.current += 1;
      clearTimeout(pollRef.current);
    };
  }, [profileId]);

  /* 本次会话还没回测时，显示上次的结论与报告（步骤条的「已完成」就是按它算的） */
  const lastRaw = (deep && deep.lastReport) || null;
  // 上次的报告不再有「后台补算」可等：没有模型评分就是没有
  const lastReport = React.useMemo(() => (lastRaw ? { ...srvNormalize(lastRaw), semanticPending: false } : null), [lastRaw]);
  const lastWhen = lastRaw ? srFormatWhen(lastRaw.finished_at || lastRaw.created_at) : null;
  const backgroundRunning = !!(deep && Array.isArray(deep.reports)
    && deep.reports.some((r) => r && (r.status === "running" || r.status === "pending")));
  const showingLast = !report && !running && !!lastReport;
  const shown = report || (showingLast ? lastReport : null);
  const showReport = (done && !!report) || showingLast;

  const run = async () => {
    if (running) return;
    setRunning(true); setDone(false); setErr(null); setErrCode(""); setReport(null); setPartial(null); setRunStartedAt(Date.now());
    clearTimeout(pollRef.current);
    const generation = ++pollGeneration.current;
    try {
      const resp = await srValidate(profileId, { text, mode });
      if (generation !== pollGeneration.current) return;
      if (resp && resp.sync_result) {
        setReport(srvNormalize(resp.sync_result)); setRunning(false); setDone(true);
        if (book) srLoadDeep(book.id, { force: true });
        return;
      }
      const rid = resp && resp.report_id;
      if (!rid) throw new Error("校验没有返回报告编号");
      const startedAt = Date.now();
      /* 2026-09-15：不再 60 秒就报「校验超时」——两次 critic 调用在慢中转上常超一分钟，服务端
         报告行有心跳与孤儿回收（10 分钟无心跳降级 failed），前端只跟报告状态；硬上限只防
         服务端彻底失联时无限轮询。 */
      const poll = async () => {
        if (generation !== pollGeneration.current) return;
        if (Date.now() - startedAt > SRV_POLL_HARD_CAP_MS) { setRunning(false); setErr("校验超过 30 分钟仍未完成，请稍后重试。"); return; }
        let rep = null;
        try { rep = await srLoadReport(rid); } catch (e) { /* 抖动下一轮 */ }
        if (generation !== pollGeneration.current) return;
        if (rep) setPartial(rep);
        if (rep && rep.verdict) {
          setReport(srvNormalize(rep)); setRunning(false); setDone(true);
          if (book) srLoadDeep(book.id, { force: true }); // 步骤条「回测校验」与「上次回测」跟上
          return;
        }
        if (rep && rep.status === "failed") {
          setRunning(false); setErr(`校验失败：${rep.error_text || "服务端没有给出原因"}`); setErrCode(rep.error_code || ""); return;
        }
        pollRef.current = setTimeout(poll, 1200);
      };
      pollRef.current = setTimeout(poll, 800);
    } catch (e) {
      if (generation !== pollGeneration.current) return;
      setRunning(false);
      setErrCode((e && e.code) || "");
      setErr(e && (e.code === "STYLE_REFERENCE_LLM_REQUIRED" || e.code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED")
        ? "完整校验的模型评分需要先接入模型；可以改用「快速」（量化 + 抄袭，不需要模型）。"
        : ((e && e.message) || "回测失败"));
    }
  };

  const verdict = (() => {
    if (running) return { kind: "idle", label: "回测中…", sub: "结论出来后显示在这里" };
    if (report) return srvVerdictMeta(report.verdict);
    // 步骤条此时写「进行中」：这里也先说有一次回测在后台跑，下面仍可看上一份报告
    if (backgroundRunning) return { kind: "idle", label: "回测进行中", sub: "之前发起的回测还在后台跑，完成后这里显示结论" };
    if (showingLast) {
      const meta = srvVerdictMeta(lastReport.verdict);
      return { kind: meta.kind, label: `上次回测：${meta.label}`, sub: `${lastWhen ? `${lastWhen} · ` : ""}本次还没回测` };
    }
    return { kind: "idle", label: "还没回测", sub: "粘贴一段文字后运行回测" };
  })();

  // 四路汇总（本次的报告，或上次的）
  const sum = shown ? {
    quant: shown.quant.length ? Math.round(shown.quant.filter(q => q.passed).length / shown.quant.length * 100) + "%" : "—",
    semantic: shown.semantic.length ? (shown.semantic.reduce((s, x) => s + x.score, 0) / shown.semantic.length).toFixed(1) : (shown.semanticPending ? "后台补算" : "—"),
    plag: shown.plagiarism.passed ? "通过" : "命中",
    forbidden: shown.forbidden.length,
  } : null;

  // 改写建议：触发的禁忌 + 偏离最大的量化项
  const rewriteHints = shown ? (() => {
    const hints = [];
    shown.forbidden.slice(0, 2).forEach(f => hints.push({ tone: "warn", label: "禁忌", text: `触发「${f.statement}」${f.excerpt ? `：「${f.excerpt}」` : ""}，建议改成具体的动作或物件。` }));
    const worst = shown.quant.filter(q => !q.passed).sort((a, b) => (b.deviation || 0) - (a.deviation || 0))[0];
    if (worst) hints.push({ tone: "info", label: "量化", text: `${worst.name}实测 ${worst.pct ? srFormatPct(worst.actual) : (Math.round(worst.actual * 10) / 10)}，偏离目标 ${(worst.deviation || 0).toFixed(2)} 倍容差。` });
    return hints;
  })() : null;

  /* 还没有画像：回测无对象，空态引导 */
  if (!realMode) {
    return (
      <div className="card sr-stage-empty-card">
        <EmptyState icon="Beaker" title="还不能回测" actions={<button type="button" className="btn btn-accent btn-sm" onClick={() => go && go("matrix")}>去维度矩阵</button>}>
          回测把一段文字对照「风格画像」做量化、语义、抄袭和禁忌检查。先在「维度矩阵」完成抽取并合成画像。
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="srv">
      <div className="srv-main">
        <div className="card">
          <div className="card-head">
            <div><div className="card-title">回测输入</div><div className="card-sub">用「{profileTitle}」检查一段文字写得像不像</div></div>
            <Segmented
              label="校验方式"
              value={mode}
              onChange={setMode}
              options={[
                { value: "sync_only", label: "快速（量化 + 抄袭）" },
                { value: "async_full", label: "完整（四路，含模型评分）" },
              ]}
            />
          </div>
          <label className="label" htmlFor={textId}>要回测的文字</label>
          <textarea
            id={textId}
            className="srv-input textarea"
            value={text}
            placeholder="粘贴一段按这份画像写出来的文字，几百字以上结果更可靠。"
            aria-describedby={hintId}
            onChange={e => setText(e.target.value)}
          />
          <div className="srv-input-foot">
            <p className="srv-mode-hint" id={hintId}>
              {mode === "sync_only"
                ? <><I.Zap size={12} /> 只做量化与抄袭检查，几秒内返回，不需要模型。</>
                : <><I.Beaker size={12} /> 量化、抄袭、禁忌与模型评分四路一起跑，模型评分通常要一两分钟。</>}
            </p>
            <button type="button" className="btn btn-accent" onClick={run} disabled={running || !text.trim()} title={!text.trim() ? "先粘贴要回测的文字" : undefined}>
              {running ? <><Spinner size={13} /> 回测中…</> : <><I.Play size={13} /> 运行回测</>}
            </button>
          </div>
          {err && <Notice tone="danger" className="srv-error"><span title={errCode ? `错误代码：${errCode}` : undefined}>{err}</span></Notice>}
        </div>

        {running && (() => {
          const rows = srvRunningRows(partial, mode);
          const elapsed = runStartedAt ? Math.max(0, (Date.now() - runStartedAt) / 1000) : 0;
          return (
            <div className="card srv-running" data-testid="srv-running">
              <div className="srv-run-rows">
                {rows.map((row) => (
                  <div key={row.id} className={`srv-run-row${row.done ? " is-done" : ""}`} data-testid={`srv-run-${row.id}`} data-done={row.done ? "1" : "0"}>
                    {row.done ? <I.Check size={13} className="srv-run-ok" /> : <Spinner size={14} />}
                    <span>{row.label}</span>
                  </div>
                ))}
              </div>
              <div className="srv-run-meta">
                已用 {srFormatDuration(elapsed)}{partial && partial.status === "pending" ? " · 排队中" : ""}{mode === "async_full" ? " · 模型评分在慢的中转上常要一两分钟" : ""}
              </div>
            </div>
          );
        })()}
        {showReport && !running && showingLast && (
          <SectionLabel icon="Clock" className="srv-last-label" aside={lastWhen || undefined}>上次回测的报告</SectionLabel>
        )}
        {showReport && !running && <ValidationReportCard report={shown} mode={mode} />}
      </div>

      <aside className="srv-side">
        <div className={`srv-verdict v-${verdict.kind}`} role="status" data-testid="srv-verdict">
          <div className="srv-verdict-icon" aria-hidden="true">
            {verdict.kind === "pass" && <I.CheckCircle size={26} />}
            {verdict.kind === "partial" && <I.AlertTriangle size={26} />}
            {verdict.kind === "fail" && <I.X size={26} />}
            {verdict.kind === "plagiarism" && <I.Ban size={26} />}
            {verdict.kind === "idle" && <I.Beaker size={24} />}
          </div>
          <div className="srv-verdict-label">{verdict.label}</div>
          <div className="srv-verdict-sub">{verdict.sub}</div>
        </div>

        {sum && (
          <div className="card-flat">
            <SectionLabel icon="Target">四路汇总</SectionLabel>
            <ul className="srv-summary">
              <li><span>量化对齐</span><b className="srv-sum-val">{sum.quant}</b></li>
              <li><span>语义评分</span><b className="srv-sum-val">{sum.semantic}</b></li>
              <li><span>抄袭检测</span><b className={`srv-sum-val ${sum.plag === "通过" ? "ok" : "warn"}`}>{sum.plag}</b></li>
              <li><span>禁忌触发</span><b className={`srv-sum-val ${sum.forbidden > 0 ? "warn" : "ok"}`}>{sum.forbidden} 项</b></li>
            </ul>
          </div>
        )}

        {rewriteHints && rewriteHints.length > 0 && (
          <div className="card-flat srv-rewrite">
            <SectionLabel icon="Wand">改写建议</SectionLabel>
            {rewriteHints.map((h, i) => (
              <div key={i} className="srv-rewrite-item">
                <Tag tone={h.tone} dot>{h.label}</Tag>
                <p>{h.text}</p>
              </div>
            ))}
            <p className="srv-rewrite-note">部分通过时，起草流程会自动带着修改意见重试（最多 2 轮）；未通过或疑似抄袭会交给你审核。</p>
          </div>
        )}

        <button type="button" className="btn btn-accent btn-lg srv-next" onClick={() => go && go("apply")}>
          进入注入应用 <I.ArrowRight size={15} />
        </button>
      </aside>
    </div>
  );
}

/* ============ ValidationReportCard ============ */
export function ValidationReportCard({ report, mode }) {
  if (!report) return null;
  const quant = report.quant;
  const quantPass = quant.filter(quantItemPass).length;
  const plag = report.plagiarism;
  const forbidden = report.forbidden;
  const forbiddenHits = forbidden.filter(f => f.triggered).length;
  const semantic = report.semantic;
  const semanticPending = report.semanticPending;
  const semanticMean = semantic.length ? (semantic.reduce((s, d) => s + (d.score != null ? d.score : d.v * 10), 0) / semantic.length) : null;

  return (
    <div className="vrc">
      {/* Quantitative */}
      <div className="card">
        <div className="card-head">
          <div><div className="card-title">量化对齐</div><div className="card-sub">容差是基线波动的 1.25 倍，每项另有最小值</div></div>
          <Tag tone={quant.length && quantPass === quant.length ? "ok" : "warn"} dot>{quantPass} / {quant.length} 通过</Tag>
        </div>
        {quant.length === 0 ? (
          <p className="sr-ov-text sr-ov-muted">这份画像没有统计基线，无法做量化对齐。</p>
        ) : (
          <div className="vrc-quant">
            {quant.map((m, i) => <QuantBar key={i} m={m} />)}
          </div>
        )}
      </div>

      <div className="vrc-row">
        {/* Semantic radar */}
        <div className="card">
          <div className="card-head">
            <div><div className="card-title">语义评分</div><div className="card-sub">模型评审，每一项都要引用原文证据</div></div>
            {semanticPending
              ? <Tag tone="info" dot>后台补算中</Tag>
              : (semanticMean != null
                  ? <Tag tone="ok" dot>{semanticMean.toFixed(1)} / 10</Tag>
                  : <Tag dot>没有评分</Tag>)}
          </div>
          {semanticPending ? (
            <div className="vrc-async">
              <Spinner size={16} />
              <span className="sr-ov-muted">模型评分在后台补算，完成后写进这份报告。</span>
            </div>
          ) : semantic.length >= 3 ? (
            <RadarChart data={semantic} />
          ) : semantic.length > 0 ? (
            <div className="vrc-radar-legend vrc-radar-legend-solo">
              {semantic.map((d, i) => (
                <div key={i} className="vrc-radar-leg"><span className="vrc-radar-leg-name">{d.axis}</span><span className="vrc-radar-leg-val tab-num">{(d.score != null ? d.score : d.v * 10).toFixed(1)}</span></div>
              ))}
            </div>
          ) : (
            <div className="vrc-async"><span className="sr-ov-muted">这次没有产出模型评分。</span></div>
          )}
        </div>

        {/* Plagiarism */}
        <div className="card">
          <div className="card-head">
            <div><div className="card-title">抄袭检测</div><div className="card-sub">逐字比对原书，连续 {plag.threshold} 字以上相同算重叠</div></div>
            <Tag tone={plag.passed ? "ok" : "danger"} dot>{plag.passed ? "通过" : "命中"}</Tag>
          </div>
          <div className="vrc-plag-meter">
            <div className="vrc-plag-track">
              <div className={`vrc-plag-fill ${plag.passed ? "" : "is-hit"}`} style={{width: Math.min(100, (plag.maxRun / plag.threshold * 100)) + "%"}} />
              <div className="vrc-plag-threshold" />
            </div>
            <div className="vrc-plag-legend">
              <span>最长连续重叠 <b className="tab-num">{plag.maxRun}</b> 字</span>
              <span className="sr-ov-muted">阈值 {plag.threshold} 字</span>
            </div>
          </div>
          <div className="vrc-plag-flags">
            {plag.flags.length === 0 && <p className="sr-ov-text sr-ov-muted">没有超过阈值的重叠。</p>}
            {plag.flags.map((f, i) => (
              <div key={i} className={`vrc-plag-flag lv-${f.level}`}>
                <span className="vrc-plag-run">{f.run} 字</span>
                <div className="vrc-plag-body">
                  <p className="vrc-plag-text text-serif">「…{f.text}…」</p>
                  <p className="vrc-plag-src">{f.source}</p>
                </div>
                {f.level === "ok" && <Tag tone="ok" dot>安全</Tag>}
                {f.level === "hit" && <Tag tone="danger" dot>超阈值</Tag>}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Forbidden */}
      <div className="card">
        <div className="card-head">
          <div><div className="card-title">禁忌检查</div><div className="card-sub">逐条检查画像里的禁忌有没有被触发</div></div>
          <Tag tone={forbiddenHits ? "warn" : "ok"} dot>{forbiddenHits} 条触发</Tag>
        </div>
        {forbidden.length === 0 ? (
          <p className="sr-ov-text sr-ov-muted"><I.Check size={13} className="srv-run-ok" /> 没有触发任何禁忌。</p>
        ) : (
          <ul className="vrc-forbidden">
            {forbidden.map((f, i) => (
              <li key={i} className={`vrc-fb ${f.triggered ? "is-hit" : ""}`}>
                <span className="vrc-fb-mark">
                  {f.triggered ? <I.AlertTriangle size={14} /> : <I.Check size={14} />}
                </span>
                <div className="vrc-fb-body">
                  <span className="vrc-fb-statement">{f.statement}</span>
                  {f.triggered && f.excerpt && (
                    <div className="vrc-fb-hit">
                      <span className="vrc-fb-excerpt text-serif">「{f.excerpt}」</span>
                      {f.note && <span className="vrc-fb-note">{f.note}</span>}
                    </div>
                  )}
                </div>
                <Tag tone={f.triggered ? (f.severity === "error" ? "danger" : "warn") : "ok"} dot>
                  {f.triggered ? (f.severity === "error" ? "硬性触发" : "触发") : "未触发"}
                </Tag>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ---- helpers（量化通过判定）---- */
function tol(m) { return m.tolerance != null ? m.tolerance : Math.max(m.std * 1.25, 0.1); }
function quantItemPass(m) { return m.passed != null ? m.passed : Math.abs(m.actual - m.target) <= tol(m); }

/* ---- QuantBar: target band + actual marker ---- */
function QuantBar({ m }) {
  const t = tol(m);
  const range = t * 2.6;
  const lo = m.target - range, hi = m.target + range;
  const toPct = (v) => Math.max(2, Math.min(98, ((v - lo) / (hi - lo)) * 100));
  const bandLo = toPct(m.target - t), bandHi = toPct(m.target + t);
  const actualPct = toPct(m.actual);
  const pass = quantItemPass(m);
  const fmt = (v) => m.pct ? srFormatPct(v) : (Math.round(v * 10) / 10).toFixed(1);
  return (
    <div className="qbar">
      <div className="qbar-head">
        <span className="qbar-name">{m.name}</span>
        <span className={`qbar-verdict ${pass ? "ok" : "off"}`}>
          {pass ? <I.Check size={11} /> : <I.AlertTriangle size={11} />}
          实测 {fmt(m.actual)}
        </span>
      </div>
      <div className="qbar-track">
        <div className="qbar-band" style={{left: bandLo + "%", width: (bandHi - bandLo) + "%"}} />
        <div className="qbar-target" style={{left: toPct(m.target) + "%"}} />
        <div className={`qbar-actual ${pass ? "ok" : "off"}`} style={{left: actualPct + "%"}} />
      </div>
      <div className="qbar-foot">
        <span>目标 {fmt(m.target)} ± {fmt(t)}</span>
        <span className={pass ? "ok" : "off"}>偏离 {(Math.abs(m.actual - m.target) / (t || 1e-6)).toFixed(2)} 倍容差</span>
      </div>
    </div>
  );
}

/* ---- RadarChart (SVG polygon) ---- */
function RadarChart({ data }) {
  const size = 240, cx = size/2, cy = size/2, R = 86;
  const n = data.length;
  const angle = (i) => (Math.PI * 2 * i / n) - Math.PI/2;
  const pt = (i, r) => [cx + Math.cos(angle(i)) * R * r, cy + Math.sin(angle(i)) * R * r];
  const poly = data.map((d, i) => pt(i, d.v).join(",")).join(" ");
  return (
    <div className="vrc-radar">
      <svg viewBox={`0 0 ${size} ${size}`} className="vrc-radar-svg">
        {[0.25, 0.5, 0.75, 1].map((r, i) => (
          <polygon key={i}
            points={data.map((_, j) => pt(j, r).join(",")).join(" ")}
            fill="none" stroke="var(--line-2)" strokeWidth="1" />
        ))}
        {data.map((_, i) => {
          const [x, y] = pt(i, 1);
          return <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke="var(--line-1)" strokeWidth="1" />;
        })}
        <polygon points={poly} fill="var(--crimson)" fillOpacity="0.18" stroke="var(--crimson)" strokeWidth="2" strokeLinejoin="round" />
        {data.map((d, i) => {
          const [x, y] = pt(i, d.v);
          return <circle key={i} cx={x} cy={y} r="3.5" fill="var(--crimson)" stroke="var(--paper-0)" strokeWidth="1.5" />;
        })}
        {data.map((d, i) => {
          const [x, y] = pt(i, 1.18);
          return (
            <text key={i} x={x} y={y} fontSize="11" fill="var(--ink-2)" textAnchor="middle" dominantBaseline="middle" fontFamily="var(--font-sans)">{d.axis}</text>
          );
        })}
      </svg>
      <div className="vrc-radar-legend">
        {data.map((d, i) => (
          <div key={i} className="vrc-radar-leg">
            <span className="vrc-radar-leg-name">{d.axis}</span>
            <span className="vrc-radar-leg-val tab-num">{(d.score != null ? d.score : d.v * 10).toFixed(1)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
