import React from "react";
import { I } from "./icons.jsx";
import { apiGet, apiPost } from "./lib/client.js";

/* global React, I */
const { useState: useStSRV } = React;

/* ==========================================================
   风格参考 · 回测校验 stage + ValidationReportCard
   Three-way concurrent validation:
     quantitative (自适应阈值) / semantic (radar) / plagiarism (n-gram)
     + forbidden_pattern hits
   真后端：有真画像 → POST /validate（sync 内联 / async 轮询 /reports）；
   无画像 → 空态引导（不再回退演示数据）。
   ========================================================== */


/* ---- 真实 report 字段映射 ---- */
const SRV_METRIC_META = {
  avg_sentence_length: { name: "平均句长", unit: "字" },
  sentence_length_std: { name: "句长标准差", unit: "" },
  short_sentence_ratio: { name: "短句率", pct: true },
  long_sentence_ratio: { name: "长句率", pct: true },
  punctuation_density_per_1k: { name: "标点密度/千", unit: "" },
  dash_em_density_per_1k: { name: "破折号/千", unit: "" },
  ellipsis_density_per_1k: { name: "省略号/千", unit: "" },
  semicolon_density_per_1k: { name: "分号/千", unit: "" },
  question_density_per_1k: { name: "问号/千", unit: "" },
  classical_word_ratio: { name: "文言词比率", pct: true },
  colloquial_marker_ratio: { name: "口语标记率", pct: true },
  metaphor_density_per_1k: { name: "比喻密度/千", unit: "" },
  personification_density_per_1k: { name: "拟人密度/千", unit: "" },
  dialogue_ratio: { name: "对话占比", pct: true },
  psychology_ratio: { name: "心理占比", pct: true },
  description_env_ratio: { name: "环境占比", pct: true },
  description_char_ratio: { name: "人物占比", pct: true },
  action_ratio: { name: "动作占比", pct: true },
  narration_ratio: { name: "叙述占比", pct: true },
  transition_ratio: { name: "转场占比", pct: true },
  flashback_ratio: { name: "闪回占比", pct: true },
  sensory_visual_per_1k: { name: "视觉感官/千", unit: "" },
  sensory_auditory_per_1k: { name: "听觉感官/千", unit: "" },
  sensory_olfactory_per_1k: { name: "嗅觉感官/千", unit: "" },
  sensory_tactile_per_1k: { name: "触觉感官/千", unit: "" },
  sensory_gustatory_per_1k: { name: "味觉感官/千", unit: "" },
};
const SRV_SEMANTIC_AXIS = {
  language: "语言贴合", narrative: "叙事贴合", scene: "场景贴合", theme: "主题贴合",
  coherence: "连贯性", originality: "原创度", emotion: "情感基调", style: "风格贴合",
};

/* 后端 ValidationReport / report → 组件统一形状；缺数据返 null */
function srvNormalize(rep) {
  if (!rep) return null;
  const quant = (rep.quantitative_json || []).map(q => {
    const meta = SRV_METRIC_META[q.metric] || { name: q.metric, unit: "" };
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

function srvFormatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

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
  if (mode === "async_full") rows.push({ id: "semantic", label: "语义评分 · critic LLM", done: semanticDone || finished });
  rows.push({ id: "plagiarism", label: "抄袭检测 · 规范化 n-gram", done: localDone });
  rows.push({ id: "forbidden", label: "禁忌检查 · 逐条判定", done: mode === "async_full" ? finished : localDone });
  return rows;
}

function srvVerdictMeta(v) {
  switch (v) {
    case "pass": return { kind: "pass", label: "通过", sub: "四路校验达标" };
    case "plagiarism": return { kind: "plagiarism", label: "抄袭风险", sub: "最长重叠超阈值，直接进审核" };
    case "fail": return { kind: "fail", label: "未通过", sub: "触发硬性禁忌或多路不达标" };
    case "partial": return { kind: "partial", label: "部分通过", sub: "建议带修改重试一轮" };
    default: return { kind: "partial", label: "待定", sub: "" };
  }
}

export { srvRunningRows, SRV_POLL_HARD_CAP_MS };

export function SrValidation({ book, go, deepFor, loadDeep }) {
  const isReal = !!(book && book.real);
  const [deep, setDeep] = useStSRV(() => (isReal && deepFor ? deepFor(book.id) : null));
  React.useEffect(() => {
    if (!isReal) { setDeep(null); return; }
    const sync = () => setDeep(deepFor ? deepFor(book.id) : null);
    sync();
    if (loadDeep) loadDeep(book.id);
    window.addEventListener("sr:deep-changed", sync);
    return () => window.removeEventListener("sr:deep-changed", sync);
  }, [isReal, book && book.id, deepFor, loadDeep]);
  const profileId = deep && deep.profileId;
  const realMode = !!profileId;

  const [mode, setMode] = useStSRV("async_full");
  const [text, setText] = useStSRV("");
  const [running, setRunning] = useStSRV(false);
  const [report, setReport] = useStSRV(null);   // 归一化的真实报告
  const [partial, setPartial] = useStSRV(null); // running 报告的原始快照（本地三路先落库，逐路点亮）
  const [runStartedAt, setRunStartedAt] = useStSRV(null);
  const [, tick] = useStSRV(0);
  const [done, setDone] = useStSRV(false);
  const [err, setErr] = useStSRV(null);
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

  const showReport = done && !!report;

  const run = async () => {
    if (running) return;
    setRunning(true); setDone(false); setErr(null); setReport(null); setPartial(null); setRunStartedAt(Date.now());
    clearTimeout(pollRef.current);
    const generation = ++pollGeneration.current;
    try {
      const resp = await apiPost(`/api/v2/style-reference/profiles/${profileId}/validate`, {
        generated_text: text, target_kind: "manual", mode,
      });
      if (generation !== pollGeneration.current) return;
      if (resp && resp.sync_result) {
        setReport(srvNormalize(resp.sync_result)); setRunning(false); setDone(true); return;
      }
      const rid = resp && resp.report_id;
      if (!rid) throw new Error("校验未返回 report_id");
      const startedAt = Date.now();
      /* 2026-09-15：不再 60 秒就报「校验超时」——两次 critic 调用在慢中转上常超一分钟，服务端
         报告行有心跳与孤儿回收（10 分钟无心跳降级 failed），前端只跟报告状态；硬上限只防
         服务端彻底失联时无限轮询。 */
      const poll = async () => {
        if (generation !== pollGeneration.current) return;
        if (Date.now() - startedAt > SRV_POLL_HARD_CAP_MS) { setRunning(false); setErr("校验超过 30 分钟仍未完成，请稍后重试。"); return; }
        let rep = null;
        try { rep = ((await apiGet(`/api/v2/style-reference/reports/${rid}`)) || {}).report || null; } catch (e) { /* 抖动下一轮 */ }
        if (generation !== pollGeneration.current) return;
        if (rep) setPartial(rep);
        if (rep && rep.verdict) { setReport(srvNormalize(rep)); setRunning(false); setDone(true); return; }
        if (rep && rep.status === "failed") { setRunning(false); setErr(`校验失败：${rep.error_text || rep.error_code || "未知原因"}`); return; }
        pollRef.current = setTimeout(poll, 1200);
      };
      pollRef.current = setTimeout(poll, 800);
    } catch (e) {
      if (generation !== pollGeneration.current) return;
      setRunning(false);
      setErr(e && (e.code === "STYLE_REFERENCE_LLM_REQUIRED" || e.code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED")
        ? "全量三路的语义评分需启用 LLM；可改用「同步快路径」（量化 + 抄袭，无需 LLM）。"
        : ((e && e.message) || "回测失败"));
    }
  };

  const verdict = report ? srvVerdictMeta(report.verdict)
    : { kind: "partial", label: "待回测", sub: "运行回测查看结论" };

  // 四路汇总
  const sum = report ? {
    quant: report.quant.length ? Math.round(report.quant.filter(q => q.passed).length / report.quant.length * 100) + "%" : "—",
    semantic: report.semantic.length ? (report.semantic.reduce((s, x) => s + x.score, 0) / report.semantic.length).toFixed(1) : (report.semanticPending ? "异步" : "—"),
    plag: report.plagiarism.passed ? "通过" : "命中",
    forbidden: report.forbidden.length,
  } : null;

  // 真实改写建议：触发禁忌 + 最大偏离量化项
  const rewriteHints = report ? (() => {
    const hints = [];
    report.forbidden.slice(0, 2).forEach(f => hints.push({ tone: "gold", label: "禁忌", text: `触发「${f.statement}」${f.excerpt ? `：「${f.excerpt}」` : ""}，建议改具象。` }));
    const worst = report.quant.filter(q => !q.passed).sort((a, b) => (b.deviation || 0) - (a.deviation || 0))[0];
    if (worst) hints.push({ tone: "slate", label: "量化", text: `${worst.name} 实测 ${worst.pct ? (worst.actual * 100).toFixed(0) + "%" : (Math.round(worst.actual * 10) / 10)}，偏离目标 ${(worst.deviation || 0).toFixed(2)}×。` });
    return hints;
  })() : null;

  /* 真实书但还没有画像:回测无对象,空态引导 */
  if (isReal && !realMode) {
    return (
      <div className="card" style={{padding: "44px 24px", textAlign: "center"}}>
        <I.Beaker size={26} style={{color: "var(--ink-3)"}} />
        <h3 className="text-serif" style={{fontSize: 17, margin: "10px 0 6px"}}>还不能回测</h3>
        <p className="text-muted text-sm" style={{margin: "0 auto 16px", maxWidth: 420, lineHeight: 1.7}}>
          回测把生成文本对照「风格画像」做量化/语义/抄袭/禁忌校验——先在「维度矩阵」完成抽取并合成画像。
        </p>
        <button className="btn btn-accent btn-sm" onClick={() => go && go("matrix")}>去维度矩阵</button>
      </div>
    );
  }

  return (
    <div className="srv">
      <div className="srv-main">
        {/* Input */}
        <div className="card">
          <div className="card-head">
            <div><div className="card-title">回测输入</div><div className="card-sub">粘贴一段生成文本，对 {book.author}画像{realMode ? "三路并发校验" : "（演示）"}</div></div>
            <div className="seg">
              <button className={`seg-btn ${mode==="sync_only"?"is-active":""}`} onClick={()=>setMode("sync_only")}>同步快路径</button>
              <button className={`seg-btn ${mode==="async_full"?"is-active":""}`} onClick={()=>setMode("async_full")}>全量三路</button>
            </div>
          </div>
          <textarea className="srv-input textarea" value={text} onChange={e=>setText(e.target.value)} />
          <div className="srv-input-foot">
            <div className="srv-mode-hint">
              {mode === "sync_only"
                ? <span><I.Zap size={12} /> 仅量化 + 抄袭，毫秒级返回（qc 落盘 gate 用），语义后台异步补算</span>
                : <span><I.Beaker size={12} /> 量化 + 语义 + 抄袭 + 禁忌 四路并发，语义走 LLM</span>}
            </div>
            <button className="btn btn-accent" onClick={run} disabled={running || (realMode && !text.trim())}>
              {running ? <><span className="step-spin-dark" style={{width:13,height:13}} /> 回测中…</> : <><I.Play size={13} /> 运行回测</>}
            </button>
          </div>
          {err && <div className="srv-mode-hint" style={{marginTop:8, color:"var(--rose)"}}><span><I.AlertTriangle size={12} /> {err}</span></div>}
        </div>

        {/* Report */}
        {running && (() => {
          const rows = srvRunningRows(partial, mode);
          const elapsed = runStartedAt ? Math.max(0, (Date.now() - runStartedAt) / 1000) : 0;
          return (
            <div className="card srv-running" data-testid="srv-running">
              <div className="srv-run-rows">
                {rows.map((row) => (
                  <div key={row.id} className={`srv-run-row${row.done ? " is-done" : ""}`} data-testid={`srv-run-${row.id}`} data-done={row.done ? "1" : "0"}>
                    {row.done ? <I.Check size={13} style={{ color: "var(--sage)" }} /> : <span className="step-spin-dark" />}
                    <span>{row.label}</span>
                  </div>
                ))}
              </div>
              <div className="text-xs text-muted" style={{ marginTop: 8 }}>
                已用 {srvFormatDuration(elapsed)}{partial && partial.status === "pending" ? " · 排队中" : ""}{mode === "async_full" ? " · 语义路由 critic 模型评审，慢中转上常需一两分钟" : ""}
              </div>
            </div>
          );
        })()}
        {!running && realMode && !report && (
          <div className="card" style={{padding:"32px 20px", textAlign:"center"}}>
            <I.Beaker size={26} style={{color:"var(--ink-3)"}} />
            <div className="text-muted text-sm mt-2">粘贴生成文本后点「运行回测」，对该画像做{mode === "sync_only" ? "量化 + 抄袭" : "四路"}校验。</div>
          </div>
        )}
        {showReport && !running && <ValidationReportCard report={report} mode={mode} />}
      </div>

      {/* Side: verdict + summary + rewrite */}
      <aside className="srv-side">
        <div className={`srv-verdict v-${verdict.kind}`}>
          <div className="srv-verdict-icon">
            {verdict.kind === "pass" && <I.CheckCircle size={26} />}
            {verdict.kind === "partial" && <I.AlertTriangle size={26} />}
            {verdict.kind === "fail" && <I.X size={26} />}
            {verdict.kind === "plagiarism" && <I.Ban size={26} />}
          </div>
          <div className="srv-verdict-label">{verdict.label}</div>
          <div className="srv-verdict-sub">{verdict.sub}</div>
        </div>

        {sum && (
          <div className="card-flat">
            <div className="ctx-head" style={{marginBottom:10}}><I.Target size={13} /><span>四路汇总</span></div>
            <ul className="srv-summary">
              <li><span>量化对齐</span><b className="srv-sum-val ok">{sum ? sum.quant : Math.round(quantPassRate()*100) + "%"}</b></li>
              <li><span>语义评分</span><b className="srv-sum-val ok">{sum ? sum.semantic : "8.2"}</b></li>
              <li><span>抄袭检测</span><b className={`srv-sum-val ${sum ? (sum.plag === "通过" ? "ok" : "warn") : "ok"}`}>{sum ? sum.plag : "通过"}</b></li>
              <li><span>禁忌触发</span><b className={`srv-sum-val ${(sum ? sum.forbidden : 1) > 0 ? "warn" : "ok"}`}>{sum ? `${sum.forbidden} 项` : "1 项（轻）"}</b></li>
            </ul>
          </div>
        )}

        {rewriteHints && rewriteHints.length > 0 && (
          <div className="card-flat srv-rewrite">
            <div className="ctx-head" style={{marginBottom:10}}><I.Wand size={13} /><span>改写建议</span></div>
            {(rewriteHints || [
              { tone: "gold", label: "禁忌", text: "把「愈来愈浓的暮色」改为具象动作或物件，避免成语化抒情。" },
              { tone: "slate", label: "量化", text: "对话占比 5%（目标 23%±9），可在段中补一句短对话。" },
            ]).map((h, i) => (
              <div key={i} className="srv-rewrite-item">
                <span className={`pill pill-${h.tone} text-xs`}><span className="pill-dot" />{h.label}</span>
                <p>{h.text}</p>
              </div>
            ))}
            <p className="text-xs text-muted mt-2" style={{textAlign:"center"}}>partial 由生成期 qc 链路自动重试（最多 2 轮）；fail / 抄袭 直接进审核。</p>
          </div>
        )}

        <button className="btn btn-accent btn-lg" style={{width:"100%"}} onClick={() => go && go("apply")}>
          <I.ArrowRight size={15} /> 进入注入应用
        </button>
      </aside>

      <style dangerouslySetInnerHTML={{ __html: srvCss }} />
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
          <div><div className="card-title">量化对齐</div><div className="card-sub">自适应阈值 = max(σ × 1.25, 绝对下限)</div></div>
          <span className="pill pill-sage"><span className="pill-dot" />{quantPass} / {quant.length} 通过</span>
        </div>
        {quant.length === 0 ? (
          <div className="text-xs text-muted" style={{padding:"10px 2px"}}>该画像无量化基线（需先合成画像）。</div>
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
            <div><div className="card-title">语义评分</div><div className="card-sub">critic LLM · 强制引用证据</div></div>
            {semanticPending
              ? <span className="pill pill-slate text-xs"><span className="pill-dot" />异步补算中</span>
              : (semanticMean != null
                  ? <span className="pill pill-sage text-xs"><span className="pill-dot" />{semanticMean.toFixed(1)} / 10</span>
                  : <span className="pill pill-slate text-xs"><span className="pill-dot" />无评分</span>)}
          </div>
          {semanticPending ? (
            <div className="vrc-async">
              <span className="step-spin-dark" />
              <span className="text-muted text-sm">语义路径后台运行中，完成后入库供审核查看…</span>
            </div>
          ) : semantic.length >= 3 ? (
            <RadarChart data={semantic} />
          ) : semantic.length > 0 ? (
            <div className="vrc-radar-legend" style={{padding:"6px 0"}}>
              {semantic.map((d, i) => (
                <div key={i} className="vrc-radar-leg"><span className="vrc-radar-leg-name">{d.axis}</span><span className="vrc-radar-leg-val tab-num">{(d.score != null ? d.score : d.v * 10).toFixed(1)}</span></div>
              ))}
            </div>
          ) : (
            <div className="vrc-async"><span className="text-muted text-sm">本次未产出语义评分。</span></div>
          )}
        </div>

        {/* Plagiarism */}
        <div className="card">
          <div className="card-head">
            <div><div className="card-title">抄袭检测</div><div className="card-sub">规范化 n-gram · {plag.ngram}-gram · 阈值 {plag.threshold} 字</div></div>
            <span className={`pill ${plag.passed ? "pill-sage" : "pill-crimson"}`}><span className="pill-dot" />{plag.passed ? "通过" : "命中"}</span>
          </div>
          <div className="vrc-plag-meter">
            <div className="vrc-plag-track">
              <div className="vrc-plag-fill" style={{width: Math.min(100, (plag.maxRun / plag.threshold * 100)) + "%", background: plag.passed ? "var(--sage)" : "var(--crimson)"}} />
              <div className="vrc-plag-threshold" style={{left: "100%"}} />
            </div>
            <div className="vrc-plag-legend">
              <span>最长连续重叠 <b className="tab-num">{plag.maxRun}</b> 字</span>
              <span className="text-muted">阈值 {plag.threshold} 字</span>
            </div>
          </div>
          <div className="vrc-plag-flags">
            {plag.flags.length === 0 && <div className="text-xs text-muted" style={{padding:"4px 2px"}}>未发现超阈值重叠。</div>}
            {plag.flags.map((f, i) => (
              <div key={i} className={`vrc-plag-flag lv-${f.level}`}>
                <span className="vrc-plag-run">{f.run} 字</span>
                <div className="vrc-plag-body">
                  <p className="vrc-plag-text text-serif">「…{f.text}…」</p>
                  <p className="vrc-plag-src">{f.source}</p>
                </div>
                {f.level === "ok" && <span className="pill pill-sage text-xs"><span className="pill-dot" />安全</span>}
                {f.level === "hit" && <span className="pill pill-crimson text-xs"><span className="pill-dot" />超阈值</span>}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Forbidden */}
      <div className="card">
        <div className="card-head">
          <div><div className="card-title">禁忌模式检查</div><div className="card-sub">对每条 forbidden_pattern 判断是否触发</div></div>
          <span className={`pill ${forbiddenHits ? "pill-gold" : "pill-sage"}`}>
            <span className="pill-dot" />{forbiddenHits} 触发{report ? "" : ` / ${forbidden.length}`}
          </span>
        </div>
        {forbidden.length === 0 ? (
          <div className="text-xs text-muted" style={{padding:"10px 2px"}}><I.Check size={13} style={{verticalAlign:"-2px", color:"var(--sage)"}} /> 未触发任何禁忌模式。</div>
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
                <span className={`pill text-xs ${f.triggered ? (f.severity === "error" ? "pill-crimson" : "pill-gold") : "pill-sage"}`}>
                  <span className="pill-dot" />{f.triggered ? (f.severity === "error" ? "硬触发" : "触发") : "清白"}
                </span>
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
  const fmt = (v) => m.pct ? (v*100).toFixed(0) + "%" : v.toFixed(1);
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
        <span>目标 {fmt(m.target)} ± {m.pct ? (t*100).toFixed(0)+"%" : t.toFixed(1)}</span>
        <span className={pass ? "ok" : "off"}>偏离 {(Math.abs(m.actual - m.target) / (t || 1e-6)).toFixed(2)}×</span>
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

const srvCss = `
.srv { display: grid; grid-template-columns: 1fr 300px; gap: 18px; align-items: start; }
.srv-main { display: flex; flex-direction: column; gap: 16px; }
.srv-input { min-height: 96px; font-size: 15px; line-height: 1.8; }
.srv-input-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; }
.srv-mode-hint { font-size: 12px; color: var(--ink-3); display: flex; align-items: center; }
.srv-mode-hint span { display: inline-flex; align-items: center; gap: 6px; }

.srv-side { display: flex; flex-direction: column; gap: 14px; position: sticky; top: 0; }
.srv-verdict { display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 22px; border-radius: 14px; text-align: center; }
.srv-verdict.v-pass { background: var(--sage-wash); color: var(--sage); }
.srv-verdict.v-partial { background: var(--gold-wash); color: var(--gold); }
.srv-verdict.v-fail { background: var(--rose-wash); color: var(--rose); }
.srv-verdict.v-plagiarism { background: var(--crimson-wash); color: var(--crimson); }
.srv-verdict-label { font-family: var(--font-serif); font-size: 20px; font-weight: 600; margin-top: 4px; }
.srv-verdict-sub { font-size: 12.5px; opacity: 0.85; }
.srv-summary { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.srv-summary li { display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
.srv-sum-val { font-family: var(--font-serif); font-size: 15px; }
.srv-sum-val.ok { color: var(--sage); }
.srv-sum-val.warn { color: var(--gold); }
.srv-rewrite-item { display: flex; flex-direction: column; gap: 4px; padding: 10px; background: var(--paper-0); border-radius: 8px; margin-bottom: 8px; }
.srv-rewrite-item p { font-size: 12.5px; line-height: 1.5; color: var(--ink-1); }

/* ValidationReportCard */
.vrc { display: flex; flex-direction: column; gap: 16px; }
.vrc-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 16px; align-items: start; }
.vrc-quant { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px 24px; }

.qbar { display: flex; flex-direction: column; gap: 5px; }
.qbar-head { display: flex; justify-content: space-between; align-items: baseline; }
.qbar-name { font-size: 12.5px; font-weight: 600; color: var(--ink-1); }
.qbar-verdict { display: inline-flex; align-items: center; gap: 3px; font-size: 11.5px; font-variant-numeric: tabular-nums; }
.qbar-verdict.ok { color: var(--sage); }
.qbar-verdict.off { color: var(--gold); }
.qbar-track { position: relative; height: 10px; background: var(--paper-2); border-radius: 5px; }
.qbar-band { position: absolute; top: 0; bottom: 0; background: var(--sage-wash); border-radius: 3px; }
.qbar-target { position: absolute; top: -2px; bottom: -2px; width: 2px; background: var(--ink-3); border-radius: 1px; }
.qbar-actual { position: absolute; top: 50%; width: 12px; height: 12px; border-radius: 50%; transform: translate(-50%, -50%); border: 2px solid var(--paper-0); box-shadow: var(--shadow-sm); }
.qbar-actual.ok { background: var(--sage); }
.qbar-actual.off { background: var(--gold); }
.qbar-foot { display: flex; justify-content: space-between; font-size: 11px; color: var(--ink-3); }
.qbar-foot .ok { color: var(--sage); }
.qbar-foot .off { color: var(--gold); }

/* Radar */
.vrc-radar { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; }
.vrc-radar-svg { width: 190px; height: 190px; flex-shrink: 0; }
.vrc-radar-legend { display: flex; flex-direction: column; gap: 6px; flex: 1 1 150px; min-width: 140px; }
.vrc-radar-leg { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 4px 10px; background: var(--paper-0); border-radius: 6px; }
.vrc-radar-leg-name { font-size: 12.5px; color: var(--ink-2); white-space: nowrap; }
.vrc-radar-leg-val { font-family: var(--font-serif); font-weight: 600; color: var(--crimson); }
.vrc-async { display: flex; align-items: center; gap: 12px; padding: 30px 16px; justify-content: center; }
.step-spin-dark { width: 18px; height: 18px; border: 2px solid var(--line-2); border-top-color: var(--crimson); border-radius: 50%; animation: spin 0.8s linear infinite; }
.srv-running { padding: 18px 20px; }
.srv-run-rows { display: flex; flex-direction: column; gap: 12px; }
.srv-run-row { display: flex; align-items: center; gap: 12px; font-size: 13.5px; color: var(--ink-2); }
.srv-run-row .step-spin-dark { width: 15px; height: 15px; }

/* Plagiarism */
.vrc-plag-meter { margin-bottom: 14px; }
.vrc-plag-track { position: relative; height: 8px; background: var(--paper-2); border-radius: 4px; margin-bottom: 8px; }
.vrc-plag-fill { position: absolute; left: 0; top: 0; bottom: 0; background: var(--sage); border-radius: 4px; }
.vrc-plag-threshold { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--rose); }
.vrc-plag-legend { display: flex; justify-content: space-between; font-size: 12px; color: var(--ink-2); }
.vrc-plag-legend b { font-family: var(--font-serif); font-size: 14px; }
.vrc-plag-flags { display: flex; flex-direction: column; gap: 8px; }
.vrc-plag-flag { display: flex; align-items: center; gap: 10px; padding: 10px 12px; background: var(--paper-0); border: 1px solid var(--line-1); border-radius: 8px; flex-wrap: wrap; }
.vrc-plag-run { font-family: var(--font-mono); font-size: 11px; padding: 2px 7px; border-radius: 4px; background: var(--sage-wash); color: var(--sage); flex-shrink: 0; }
.vrc-plag-body { flex: 1 1 160px; min-width: 140px; }
.vrc-plag-text { font-size: 13px; color: var(--ink-1); white-space: normal; }
.vrc-plag-src { font-size: 11.5px; color: var(--ink-3); margin-top: 2px; }

/* Forbidden */
.vrc-forbidden { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.vrc-fb { display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: center; padding: 12px 14px; background: var(--paper-0); border: 1px solid var(--line-1); border-radius: 10px; }
.vrc-fb.is-hit { border-left: 3px solid var(--gold); }
.vrc-fb-mark { display: grid; place-items: center; width: 26px; height: 26px; border-radius: 999px; background: var(--sage-wash); color: var(--sage); }
.vrc-fb.is-hit .vrc-fb-mark { background: var(--gold-wash); color: var(--gold); }
.vrc-fb-body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.vrc-fb-statement { font-size: 13.5px; color: var(--ink-1); }
.vrc-fb-hit { display: flex; flex-direction: column; gap: 2px; padding: 6px 10px; background: var(--gold-wash); border-radius: 6px; }
.vrc-fb-excerpt { font-size: 13px; color: #6a4d1d; }
.vrc-fb-note { font-size: 11.5px; color: #6a4d1d; opacity: 0.85; }

@media (max-width: 1280px) {
  .srv { grid-template-columns: 1fr; }
  .srv-side { position: static; }
  .vrc-row, .vrc-quant { grid-template-columns: 1fr; }
  .vrc-radar { flex-direction: column; }
}
`;
