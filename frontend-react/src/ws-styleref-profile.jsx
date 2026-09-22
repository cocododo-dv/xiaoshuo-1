import React from "react";
import { I } from "./icons.jsx";
import { Notice, SectionLabel, Spinner, Tabs, Tag } from "./ws-ui.jsx";
import {
  SR_CONF_LABEL, SR_PARA_LABEL, computeResynthState, srDimMeta, srMetricRows, srSynthErrorMessage,
} from "./ws-styleref-model.js";
import { srPreviewSamples, srSynthesize } from "./ws-styleref-store.js";
import { SrStageEmpty, srNotify, useSrDeep } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 风格画像：概述、声音特征与叙事手法、维度摘要 / 示例预览，右侧统计基线与样例窗口
   ========================================================== */

/* 画像状态徽标：status / stale / 有新 run */
function SrProfileStatusPills({ profile, resynth }) {
  const status = profile.status || "draft";
  const statusMeta = status === "active" ? { tone: "ok", label: "已激活" }
    : status === "archived" ? { tone: "neutral", label: "已归档" }
    : { tone: "info", label: "草稿 · 应用后激活" };
  const stale = !!(profile.coverage_json && profile.coverage_json.stale);
  return (
    <span className="sr-pills">
      <Tag tone={statusMeta.tone} dot testId="sr-profile-status">{statusMeta.label}</Tag>
      {stale && <Tag tone="danger" dot testId="sr-profile-stale">已失效 · 需重新合成</Tag>}
      {!stale && resynth && resynth.reason === "new_run" && <Tag tone="warn" dot>有新的抽取结果</Tag>}
    </span>
  );
}

const SR_RESYNTH_NOTE = {
  new_run: "抽取结果更新过了，这份画像还是按旧结果合成的：重新合成后再应用。",
  stale: "审核改变了参与合成的观察，这份画像已失效，注入时不会生效：请重新合成。",
  inactive: "画像还没激活（应用并在待办里批准后自动激活）；想按最新的观察重算，可以重新合成。",
};

export function SrProfile({ book, go }) {
  const [tab, setTab] = React.useState("summary");
  const [resynthBusy, setResynthBusy] = React.useState(false);
  const deep = useSrDeep(book);
  const profile = deep && deep.profile;
  const pj = (profile && profile.profile_json) || null;

  /* 还没有画像：真实空态，不把任何示例画像当成这本书的 */
  if (!pj) {
    return (
      <SrStageEmpty title="还没有风格画像" actionLabel="去维度矩阵" onAction={() => go && go("matrix")}>
        {deep && deep.runId
          ? "抽取已经完成：回「维度矩阵」点「合成风格画像」，把 16 个维度的观察聚合成一份画像（需要模型）。"
          : "先在「维度矩阵」完成抽取，再合成风格画像。"}
      </SrStageEmpty>
    );
  }

  const cov = profile.coverage_json || {};
  const subDims = pj.sub_dimensions || null;
  const dimRows = subDims ? Object.entries(subDims).map(([path, d]) => ({
    path, ...srDimMeta(path),
    conf: (d && d.confidence) || "low", obs: (d && d.observation_count) || 0,
    fp: (d && d.forbidden_pattern_count) || 0, q: (d && d.quote_count) || 0,
  })) : [];
  const features = Array.isArray(pj.style_features) ? pj.style_features : [];
  const voiceHabits = (pj.voice_signature && Array.isArray(pj.voice_signature.habits)) ? pj.voice_signature.habits : [];
  const narrativeGuidance = Array.isArray(pj.narrative_guidance) ? pj.narrative_guidance : [];

  /* 再合成：有新 run / stale / 非 active 时显示「重新合成」并可点 */
  const resynth = computeResynthState(deep);
  const canResynth = resynth.canResynth && !!deep.runId;
  const onResynth = async () => {
    if (!canResynth || resynthBusy) return;
    setResynthBusy(true);
    try { await srSynthesize(deep.runId, book.id); }
    catch (e) { srNotify(srSynthErrorMessage(e)); }
    finally { setResynthBusy(false); }
  };

  return (
    <div className="sr-profile">
      <div className="sr-profile-main">
        <div className="card">
          <div className="card-head">
            <div>
              <div className="card-title">{profile.title || `《${book.title}》风格画像`}</div>
              <div className="card-sub">{cov.findings_count || 0} 条观察与禁忌 · 覆盖 {cov.sub_dim_count || 0} 个维度</div>
            </div>
            <SrProfileStatusPills profile={profile} resynth={resynth} />
          </div>
          {canResynth && (
            <Notice
              tone={resynth.reason === "stale" ? "warn" : "info"}
              className="sr-profile-resynth"
              testId="sr-profile-resynth"
              actions={(
                <button type="button" className="btn btn-accent btn-sm" data-testid="sr-profile-resynth-btn" disabled={resynthBusy} onClick={onResynth}>
                  {resynthBusy ? <><Spinner size={12} /> 合成中…</> : <><I.Sparkles size={13} /> 重新合成</>}
                </button>
              )}
            >
              {SR_RESYNTH_NOTE[resynth.reason] || SR_RESYNTH_NOTE.inactive}
            </Notice>
          )}
          <p className="sr-profile-summary text-serif">
            {pj.qualitative_summary || pj.narrative_summary || "这份画像没有概述。"}
          </p>
          {features.length > 0 && (
            <div className="sr-profile-features">
              {features.slice(0, 8).map((f, i) => <Tag key={i}>{f}</Tag>)}
            </div>
          )}
          {/* v2 画像新键（旧画像没有时不渲染）：声音特征习惯句 / 叙事机制 */}
          {(voiceHabits.length > 0 || narrativeGuidance.length > 0) && (
            <div className="sr-profile-v2">
              {voiceHabits.length > 0 && (
                <div className="card-flat sr-profile-list">
                  <SectionLabel icon="Quote" aside={`${voiceHabits.length} 条`}>声音特征</SectionLabel>
                  <ul>{voiceHabits.slice(0, 12).map((h, i) => <li key={i}>{h}</li>)}</ul>
                </div>
              )}
              {narrativeGuidance.length > 0 && (
                <div className="card-flat sr-profile-list">
                  <SectionLabel icon="Target" aside={`${narrativeGuidance.length} 条`}>叙事手法</SectionLabel>
                  <ul>{narrativeGuidance.slice(0, 8).map((h, i) => <li key={i}>{h}</li>)}</ul>
                </div>
              )}
            </div>
          )}

          <Tabs
            label="风格画像视图"
            idPrefix="sr-profile"
            className="sr-profile-tabs"
            value={tab}
            onChange={setTab}
            tabs={[{ id: "summary", label: "维度摘要" }, { id: "preview", label: "示例预览" }]}
          />

          {tab === "summary" && (
            <div className="sr-profile-dims" role="tabpanel" id="sr-profile-panel-summary" aria-labelledby="sr-profile-tab-summary">
              {dimRows.map((row) => (
                <div key={row.path} className="sr-pd-row">
                  <span className="sr-pd-path">{row.layer ? `${row.layer.replace("层", "")} · ` : ""}{row.name}</span>
                  <span className={`sr-pd-conf conf-dot conf-${row.conf}`} title={SR_CONF_LABEL[row.conf] || row.conf} aria-label={SR_CONF_LABEL[row.conf] || row.conf} />
                  <span className="sr-pd-counts">{row.obs} 观察 · {row.fp} 禁忌 · {row.q} 引文</span>
                </div>
              ))}
              {dimRows.length === 0 && <p className="sr-ov-text sr-ov-muted">这份画像没有维度摘要。</p>}
            </div>
          )}

          {tab === "preview" && (
            <div role="tabpanel" id="sr-profile-panel-preview" aria-labelledby="sr-profile-tab-preview">
              <SrPreview profileId={profile.profile_id} title={book.title} />
            </div>
          )}
        </div>
      </div>

      <SrProfileSide pj={pj} onNext={() => go && go("validation")} />
    </div>
  );
}

/* 右栏：统计基线、原文样例窗口、下一步 */
function SrProfileSide({ pj, onNext }) {
  const baseline = srMetricRows(pj.metrics_baseline);
  const exemplar = (pj.exemplar_windows && typeof pj.exemplar_windows === "object") ? pj.exemplar_windows : null;
  return (
    <aside className="sr-profile-side">
      <div className="card-flat">
        <SectionLabel icon="Target">统计基线</SectionLabel>
        <div className="sr-baseline">
          {baseline.map((m) => (
            <div key={m.key} className="sr-baseline-row">
              <span>{m.name}</span>
              <span className="tab-num"><b>{m.value}</b>{m.unit ? ` ${m.unit}` : ""} {m.spread && <span className="sr-baseline-spread">{m.spread}</span>}</span>
            </div>
          ))}
          {baseline.length === 0 && <p className="sr-ov-text sr-ov-muted">这份画像没有统计基线。</p>}
        </div>
        <p className="sr-side-note">回测时实测值落在「基线 ± 1.25 倍波动」内就算贴合，每项另有最小容差。</p>
      </div>

      <div className="card-flat">
        <SectionLabel icon="Quote">原文样例窗口</SectionLabel>
        {exemplar && exemplar.window_count ? (
          <p className="sr-side-text">
            全书切成 <b className="tab-num">{Number(exemplar.window_count).toLocaleString()}</b> 个连续段落窗口
            {exemplar.chapter_count ? `（${exemplar.chapter_count} 章）` : ""}。起草每个场景时按强度轮换取其中几个作示范，不同场景看到不同的窗口。
          </p>
        ) : (
          <p className="sr-side-text">这份画像是旧版合成的：样例窗口在第一次注入时按需建立，不影响使用。</p>
        )}
      </div>

      <button type="button" className="btn btn-accent btn-lg sr-side-next" onClick={onNext}>进入回测校验 <I.ArrowRight size={15} /></button>
    </aside>
  );
}

/* 示例预览：作者点了才生成（三次模型调用，一两分钟），结果按画像缓存在本页会话里，
   切页签回来不会重新生成。 */
const SR_PREVIEW_CACHE = new Map(); // profileId -> { samples, at }
const SR_VERDICT_META = {
  pass:       { tone: "ok",     label: "回测通过" },
  partial:    { tone: "warn",   label: "部分通过" },
  fail:       { tone: "danger", label: "未通过" },
  plagiarism: { tone: "danger", label: "疑似抄袭" },
};

function SrPreview({ profileId, title }) {
  const cached = (id) => (id ? SR_PREVIEW_CACHE.get(id) || null : null);
  const [samples, setSamples] = React.useState(() => { const c = cached(profileId); return c ? c.samples : null; });
  const [at, setAt] = React.useState(() => { const c = cached(profileId); return c ? c.at : null; });
  const [loading, setLoading] = React.useState(false);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => {
    const c = cached(profileId);
    setSamples(c ? c.samples : null);
    setAt(c ? c.at : null);
    setErr(null);
  }, [profileId]);
  const run = () => {
    if (!profileId || loading) return;
    setLoading(true); setErr(null); setSamples([]);
    srPreviewSamples(profileId, { title, onSample: (list) => setSamples(list) })
      .then((r) => {
        const list = (r && r.samples) || [];
        const stamp = Date.now();
        SR_PREVIEW_CACHE.set(profileId, { samples: list, at: stamp });
        setSamples(list); setAt(stamp);
      })
      .catch((e) => setErr(e && (e.code === "STYLE_REFERENCE_LLM_REQUIRED" || e.code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED")
        ? "生成示例需要先接入模型（设置 → 模型与接入）。"
        : `生成失败：${(e && e.message) || "原因不明"}`))
      .finally(() => setLoading(false));
  };

  const list = (samples || []).map((s) => ({ kind: SR_PARA_LABEL[s.paragraph_type] || s.paragraph_type, verdict: s.verdict || "partial", text: s.sample_text || "", error: s.error }));
  const stamp = at ? new Date(at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : null;

  return (
    <div className="sr-preview">
      <div className="sr-preview-head">
        <span className="sr-preview-hint">
          {stamp && !loading ? `${stamp} 生成的示例` : "按对话、环境、心理各写一段示例并自动回测。要调用三次模型，通常一两分钟。"}
        </span>
        <button type="button" className="btn btn-ghost btn-sm" disabled={loading || !profileId} onClick={run}>
          {loading ? <><Spinner size={12} /> 生成中 {(samples || []).length}/3…</> : samples && samples.length ? <><I.Refresh size={13} /> 重新生成</> : <><I.Sparkles size={13} /> 生成 3 段示例</>}
        </button>
      </div>
      {err && <Notice tone="danger">{err}</Notice>}
      {list.map((s, i) => {
        const verdict = SR_VERDICT_META[s.verdict] || SR_VERDICT_META.partial;
        return (
          <article key={i} className="sr-pv-card">
            <header className="sr-pv-head">
              <Tag>{s.kind}</Tag>
              {s.error ? <Tag tone="danger" dot>生成失败</Tag> : <Tag tone={verdict.tone} dot>{verdict.label}</Tag>}
            </header>
            {s.text && <p className="sr-pv-text text-serif">{s.text}</p>}
          </article>
        );
      })}
    </div>
  );
}
