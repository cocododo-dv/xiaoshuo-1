import React from "react";
import { I } from "./icons.jsx";
import { EmptyState, Notice, Spinner, Tag } from "./ws-ui.jsx";
import { SR_PARA_LABEL, SR_SCOPE_LABEL, SR_SCOPE_TONE, srDraftModeLabel, srStrategyName } from "./ws-styleref-model.js";

/* ==========================================================
   风格参考 · 注入内容的只读视图（注入应用页用）
   · SrLayersReal：当前作品起草时实际叠加的各层（GET /injection/layers）
   · SrBundleReal：起草时实际带上的内容（dryrun 注入预览的各块 + 构成条）
   · SrSampleWindows：本次注入取了原书的哪些连续窗口
   只收 props，不发请求。
   ========================================================== */

/* 叠加注入层：命中层 + 权重 / 预算 + 合并概要 */
export function SrLayersReal({ stack, err, bindings = [], targetLabel }) {
  if (err) {
    return <div className="card"><Notice tone="danger" title="读不到叠加层">{err}</Notice></div>;
  }
  if (!stack) {
    return <div className="card"><div className="sr-ov-text sr-ov-muted"><Spinner size={12} /> 正在解析当前作品的叠加层…</div></div>;
  }
  const layers = stack.layers || [];
  const total = stack.budget_total || 800;
  const merged = stack.merged;
  if (layers.length === 0) {
    return (
      <div className="card">
        <EmptyState icon="Layers" compact title="当前作品还没有生效的绑定">
          在「策略与维度」应用画像并在待办里批准后，这里显示起草时实际叠加的各层。
        </EmptyState>
      </div>
    );
  }
  const maxBudget = Math.max(...layers.map((l) => l.budget_chars || 0), 1);
  return (
    <div className="card">
      <div className="card-head">
        <div><div className="card-title">叠加注入层</div><div className="card-sub">越具体的层分到的篇幅越多：场景 &gt; 角色 &gt; 项目 &gt; 全局</div></div>
        <Tag tone="ok" dot>{layers.length} 层 · 预算 {total} 字</Tag>
      </div>

      <div className="sr-stack">
        {layers.map((l) => {
          const tone = SR_SCOPE_TONE[l.scope] || "neutral";
          // 叠层端点不带 config_json：起草方式从本画像的绑定表按 binding_id 对位，对不上（他画像的层）就不猜
          const own = (bindings || []).find((b) => b && b.binding_id === l.binding_id);
          const target = targetLabel ? targetLabel(l.scope, l.scope_ref_id) : (l.scope_ref_id || "—");
          return (
            <div key={l.binding_id} className="sr-stack-layer">
              <Tag tone={tone} className="sr-stack-rank">{SR_SCOPE_LABEL[l.scope] || l.scope}</Tag>
              <div className="sr-stack-body">
                <div className="sr-stack-top">
                  <span className="sr-stack-target text-serif">{l.profile_title || "未命名画像"}</span>
                  <span className="sr-stack-sub" title={l.scope_ref_id || undefined}>{target} · {srStrategyName(l.strategy)}</span>
                  {own && <Tag testId="sr-layer-draft-mode" title="起草方式">{srDraftModeLabel(own.config_json && own.config_json.draft_mode)}</Tag>}
                  <span className="sr-stack-frags">{l.fragment_count} 段内容</span>
                </div>
                <div className="sr-stack-budget">
                  <div className="sr-stack-budget-track">
                    <div className={`sr-stack-budget-fill fill-${tone}`} style={{ width: ((l.budget_chars || 0) / maxBudget * 100) + "%" }} />
                  </div>
                  <span className="sr-stack-weight">权重 ×{l.weight}</span>
                  <span className="sr-stack-tokens tab-num">{l.budget_chars} 字</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <p className="sr-stack-note">
        {merged && merged.layer_count > 1
          ? <>{merged.layer_count} 层合并成一份，策略取最具体的一层（{srStrategyName(merged.strategy)}），起草时带上 <b className="tab-num">{merged.prefix_chars}</b> 字。重复的禁忌只留一条，统计倾向取最具体的一层，样例窗口不叠加。</>
          : <>只有一层：按它自己的策略完整带上，不做叠加截断，起草时带上 <b className="tab-num">{merged ? merged.prefix_chars : 0}</b> 字。</>}
      </p>
    </div>
  );
}

/* 预览还没到 / 失败时的占位（起草内容与样例窗口共用） */
function SrPreviewPending({ previewErr }) {
  if (previewErr) return <Notice tone="danger" title="预览失败">{previewErr}</Notice>;
  return <div className="sr-ov-text sr-ov-muted"><Spinner size={12} /> 正在生成预览…</div>;
}

/* 注入前缀的各块：顺序与后端 schemas.to_system_prompt_prefix 一致（2026-09-09 样例优先）：
   few_shot → rag → voice → positive → forbidden → metric，最后是固定的防抄袭红线。 */
const SR_PREFIX_BLOCKS = [
  { key: "few_shot_block", label: "原文样例", tone: "sample" },
  { key: "rag_block", label: "相近片段", tone: "sample" },
  { key: "voice_block", label: "声音特征", tone: "rule" },
  { key: "positive_block", label: "正向手法", tone: "rule" },
  { key: "forbidden_block", label: "禁忌", tone: "danger", tech: "banned_pattern_block" },
  { key: "metric_anchor_block", label: "统计倾向", tone: "rule" },
];

/* 真实注入预览（dryrun）：每块的字数 + 构成条（样例 vs 规则），每块可展开看全文。 */
export function SrBundleReal({ preview, previewErr }) {
  if (previewErr || !preview) return <SrPreviewPending previewErr={previewErr} />;
  const f = preview.fragments || {};
  const present = SR_PREFIX_BLOCKS
    .map((b) => ({ ...b, text: String(f[b.key] || "").trim() }))
    .filter((b) => b.text);
  const anti = String(f.anti_plagiarism_block || "").trim();
  if (!present.length && !anti) {
    return <p className="sr-ov-text sr-ov-muted">这份画像还没有可注入的内容：先抽取并合成出观察再应用。</p>;
  }
  const blocks = anti ? [...present, { key: "anti_plagiarism_block", label: "防抄袭红线", tone: "lock", text: anti }] : present;
  const stats = preview.stats && typeof preview.stats === "object" ? preview.stats : null;
  const sum = blocks.reduce((s, b) => s + b.text.length, 0) || 1;
  const prefixLen = stats && Number.isFinite(Number(stats.total_prefix_chars)) ? Number(stats.total_prefix_chars) : (preview.prefix || "").length;
  const sampleChars = blocks.filter((b) => b.tone === "sample").reduce((s, b) => s + b.text.length, 0);
  const samplePct = Math.round(sampleChars / sum * 100);
  return (
    <div className="sr-bundle-body">
      <div className="sr-compose" role="img" aria-label={`共 ${prefixLen} 字，其中原文样例约 ${samplePct}%`}>
        {blocks.map((b) => (
          <span key={b.key} className={`sr-compose-seg tone-${b.tone}`} style={{ flexGrow: Math.max(b.text.length, 1) }} title={`${b.label} ${b.text.length.toLocaleString()} 字`} />
        ))}
      </div>
      <div className="sr-compose-legend">
        <span className="tab-num"><b>{prefixLen.toLocaleString()}</b> 字</span>
        <span>样例 {samplePct}% · 规则 {100 - samplePct}%</span>
      </div>
      <ol className="sr-frag-list">
        {blocks.map((b, i) => (
          <li key={b.key} className={`sr-bundle-frag tone-${b.tone}`} data-block={b.key}>
            <details>
              <summary title={b.tech || b.key}>
                <span className="sr-frag-ord">{b.tone === "lock" ? <I.ShieldCheck size={10} /> : i + 1}</span>
                <span className="sr-frag-name">{b.label}{b.tone === "lock" ? " · 固定" : ""}</span>
                <span className="sr-frag-chars tab-num">{b.text.length.toLocaleString()} 字</span>
              </summary>
              <pre className="sr-frag-text">{b.text}</pre>
            </details>
          </li>
        ))}
      </ol>
    </div>
  );
}

/* 样例窗口页签：本次注入取了原书的哪些连续窗口（来自预览的 window_refs + few_shot_block）。 */
const SR_POSITION_LABEL = { opening: "章首", closing: "章末", middle: "章中", whole: "整章" };

/* few_shot_block 按「- (…)」开头的行切成各窗口正文（第一段是块标题，丢掉） */
function srSplitWindows(text) {
  const parts = String(text || "").split(/\n(?=- \()/);
  return parts.slice(1).map((p) => p.replace(/^- \([^)]*\)/, "").trim());
}

export function SrSampleWindows({ preview, previewErr, strategy }) {
  const stats = preview && preview.stats && typeof preview.stats === "object" ? preview.stats : null;
  const refs = preview && Array.isArray(preview.window_refs) ? preview.window_refs : [];
  const texts = srSplitWindows(preview && preview.fragments && preview.fragments.few_shot_block);
  const windows = refs.length ? refs.map((r, i) => ({ ...r, text: texts[i] || "" })) : texts.map((t) => ({ text: t }));
  return (
    <div className="card">
      <div className="card-head">
        <div><div className="card-title">样例窗口</div><div className="card-sub">从原书整段摘取的连续段落；每个场景起草时按强度轮换，不同场景看到不同的窗口</div></div>
        {stats && <Tag tone="info">{Number(stats.few_shot_windows) || 0} 个窗口 · {(Number(stats.few_shot_chars) || 0).toLocaleString()} 字</Tag>}
      </div>
      {strategy === "A" && <Notice tone="info">当前策略只用规则，不带原文样例。改选「规则 + 样例」或「只用样例」即可启用。</Notice>}
      {strategy === "C" && <Notice tone="info">当前策略用「相近片段」：样例由检索提供，不是这里的窗口。</Notice>}
      {previewErr || !preview ? (
        <SrPreviewPending previewErr={previewErr} />
      ) : windows.length === 0 ? (
        (strategy === "B" || strategy === "mixed")
          ? <p className="sr-ov-text sr-ov-muted">这份画像暂时没有可用的样例窗口：重跑抽取后重新合成画像可以补齐。</p>
          : null
      ) : (
        <ol className="sr-window-list">
          {windows.map((w, i) => (
            <li key={i} className="sr-window">
              <details>
                <summary>
                  <span className="sr-window-meta">
                    {w.chapter ? `第 ${w.chapter} 章` : `窗口 ${i + 1}`}
                    {w.position ? ` · ${SR_POSITION_LABEL[w.position] || w.position}` : ""}
                    {w.paragraph_type ? ` · 以${SR_PARA_LABEL[w.paragraph_type] || w.paragraph_type}为主` : ""}
                  </span>
                  {w.chars ? <span className="sr-window-size tab-num">{w.paragraphs ? `${w.paragraphs} 段 · ` : ""}{Number(w.chars).toLocaleString()} 字</span> : null}
                  <span className="sr-window-excerpt text-serif">{w.text.slice(0, 60)}{w.text.length > 60 ? "…" : ""}</span>
                </summary>
                <p className="sr-window-text text-serif">{w.text}</p>
              </details>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
