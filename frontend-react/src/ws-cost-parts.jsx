import React from "react";
import { I } from "./icons.jsx";
import { StatCard } from "./ws-quality-ui.jsx";
import { Tag } from "./ws-ui.jsx";
import { accountingStatusMeta, chapterLabelById, llmNodeLabel, sceneLabelById } from "./ws-labels.js";
import { PHASE_LABEL, costBack } from "./ws-cost-store.js";

/* ==========================================================
   成本看板的展示件（从 ws-cost.jsx 拆出，2026-09-22）
   格式化、趋势柱状图、阶段 / 节点 / 模型 / 章节 / 调用明细、额度、口径说明、下钻面板。
   只画不取数：数据都由 ws-cost.jsx 从 store 快照里传进来。
   ========================================================== */

/* ---- 格式化 ---- */
/* 金额统一两位小数；不足 0.01 的非零小额才给四位（否则显示成 0.00）。 */
export function fmtMoney(v, cur) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const digits = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2;
  return `${n.toFixed(digits)} ${cur || "USD"}`;
}
export function fmtInt(v) { return v === null || v === undefined ? "—" : Number(v).toLocaleString(); }
function fmtPct(v) { return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`; }
function fmtDay(d) { return (d || "").slice(5); }
function fmtTime(iso) { return (iso || "").replace("T", " ").slice(5, 16); }

/* ==========================================================
   小部件
   ========================================================== */

export function EstimatePill({ on }) {
  if (!on) return null;
  return <Tag tone="warn" dot title="价格来自占位估算单价（config/pricing.yaml），不是真实账单">估算价</Tag>;
}

/* 模型节点：有中文名给中文名，没有就等宽显示原始 id（机器标识）；原始 id 总在 title 里 */
function NodeName({ nodeId }) {
  const label = llmNodeLabel(nodeId);
  if (!nodeId) return <span>—</span>;
  return label
    ? <span className="cs-node" title={nodeId}>{label}</span>
    : <code className="cs-node is-raw" title={nodeId}>{nodeId}</code>;
}

/* 逐日费用柱状图：后端稠密补零序列，纯 SVG，无第三方依赖 */
export function TrendChart({ trend, currency }) {
  const series = (trend && trend.series) || [];
  if (!series.length) return null;
  const W = 720, H = 110, PADX = 2;
  const slot = (W - PADX * 2) / series.length;
  const bw = Math.max(2, slot - 2);
  const maxCost = Math.max(...series.map((d) => d.cost || 0));
  const scale = maxCost > 0 ? (H - 12) / maxCost : 0;
  const activeDays = series.filter((d) => (d.call_count || 0) > 0).length;
  return (
    <div>
      <svg className="cs-trend" viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none"
           role="img" aria-label={`近 ${series.length} 天逐日成本柱状图`}>
        {series.map((d, i) => {
          const h = (d.cost || 0) > 0 ? Math.max(2, d.cost * scale) : 1;
          return (
            <rect key={d.date} x={PADX + i * slot} y={H - h} width={bw} height={h} rx={1}
                  className={(d.call_count || 0) > 0 ? "cs-bar is-on" : "cs-bar"}>
              <title>{`${d.date} · ${fmtMoney(d.cost, currency)}（${fmtInt(d.call_count)} 次）`}</title>
            </rect>
          );
        })}
      </svg>
      <div className="cs-trend-axis">
        <span>{fmtDay(series[0].date)}</span>
        <span title={`${fmtInt(trend.window_tokens)} token`}>
          {activeDays === 0 ? "窗口内暂无调用" : `合计 ${fmtMoney(trend.window_cost, currency)} · ${fmtInt(trend.window_call_count)} 次调用`}
        </span>
        <span>{fmtDay(series[series.length - 1].date)}</span>
      </div>
    </div>
  );
}

function MeterBar({ ratio, danger }) {
  const pct = Math.round(Math.min(1, Math.max(0, ratio || 0)) * 100);
  return (
    <div className="cs-meter">
      <div className={`cs-meter-fill${danger ? " is-danger" : ""}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function PhaseBars({ breakdown, currency }) {
  const rows = Object.keys(PHASE_LABEL)
    .map((k) => ({ k, tokens: 0, cost: 0, share: 0, call_count: 0, ...((breakdown || {})[k] || {}) }))
    .filter((r) => (r.call_count || 0) > 0);
  if (!rows.length) return <p className="cs-none">暂无阶段成本。</p>;
  return (
    <div className="cs-bars">
      {rows.map((r) => (
        <div key={r.k}>
          <div className="cs-bar-row" title={`${fmtInt(r.call_count)} 次调用`}>
            <span>{PHASE_LABEL[r.k]}</span>
            <span className="cs-bar-val">{fmtMoney(r.cost, currency)} · {fmtPct(r.share)}</span>
          </div>
          <div role="progressbar" aria-label={`${PHASE_LABEL[r.k]}占比`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round((r.share || 0) * 100)}>
            <MeterBar ratio={r.share} />
          </div>
        </div>
      ))}
    </div>
  );
}

function TableScroller({ label, children }) {
  return <div className="cs-table-scroll" role="region" aria-label={label} tabIndex={0}>{children}</div>;
}

export function ModelTable({ byModel, currency }) {
  if (!byModel || !byModel.length) return <p className="cs-none">暂无模型调用。</p>;
  return (
    <TableScroller label="按模型的成本构成">
      <table className="lib-table cs-table">
        <thead>
          <tr><th>提供商</th><th>模型</th><th className="is-num">费用</th><th className="is-num">Token</th><th className="is-num">调用</th><th>价源</th></tr>
        </thead>
        <tbody>
          {byModel.map((m) => (
            <tr key={`${m.provider}:${m.model}`}>
              <td>{m.provider}</td>
              <td>{m.model}</td>
              <td className="is-num">{fmtMoney(m.cost, currency)}</td>
              <td className="is-num">{fmtInt(m.tokens)}</td>
              <td className="is-num">{fmtInt(m.call_count)}</td>
              <td>{m.is_estimate ? <Tag tone="warn">估算</Tag> : <Tag tone="info">实际</Tag>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroller>
  );
}

export function NodeBars({ byNode, currency }) {
  const top = (byNode && byNode.top) || [];
  if (!top.length) return <p className="cs-none">暂无节点成本。</p>;
  const max = Math.max(...top.map((n) => n.cost || 0), 1e-12);
  const remainder = byNode.remainder;
  return (
    <div className="cs-bars">
      {top.map((n) => (
        <div key={n.node_id}>
          <div className="cs-bar-row">
            <span className="cs-bar-name">
              <NodeName nodeId={n.node_id} />
              <Tag tone="neutral">{PHASE_LABEL[n.phase] || "其他"}</Tag>
            </span>
            <span className="cs-bar-val">{fmtMoney(n.cost, currency)} · {fmtInt(n.call_count)} 次</span>
          </div>
          <MeterBar ratio={(n.cost || 0) / max} />
        </div>
      ))}
      {remainder && (
        <p className="cs-none">
          其余 {fmtInt(remainder.node_count)} 个节点合计 {fmtMoney(remainder.cost, currency)}（{fmtInt(remainder.call_count)} 次）
        </p>
      )}
    </div>
  );
}

export function ChapterTable({ byChapter, chapters, currency, onDrill, loading }) {
  if (!byChapter || !byChapter.length) return <p className="cs-none">暂无章节成本——生成过草稿的章节会出现在这里。</p>;
  return (
    <TableScroller label="按章节的成本构成">
      <table className="lib-table cs-table">
        <thead>
          <tr><th>章节</th><th className="is-num">费用</th><th className="is-num">Token</th><th className="is-num">调用</th><th className="is-num">涉及场景</th><th><span className="ws-sr-only">明细</span></th></tr>
        </thead>
        <tbody>
          {byChapter.map((c) => (
            <tr key={c.chapter_id || "__none__"}>
              <td title={c.chapter_id || undefined}>{chapterLabelById(chapters, c.chapter_id)}</td>
              <td className="is-num">{fmtMoney(c.cost, currency)}</td>
              <td className="is-num">{fmtInt(c.tokens)}</td>
              <td className="is-num">{fmtInt(c.call_count)}</td>
              <td className="is-num">{fmtInt(c.scene_count)}</td>
              <td className="is-act">
                {c.chapter_id && (
                  <button type="button" className="btn btn-ghost btn-sm" disabled={loading} onClick={() => onDrill(c.chapter_id)}>
                    看明细 <I.ChevronRight size={12} />
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroller>
  );
}

export function TopCallsTable({ topCalls, chapters, currency, onDrillScene, loading }) {
  if (!topCalls || !topCalls.length) return <p className="cs-none">暂无调用明细。</p>;
  return (
    <TableScroller label="费用最高的调用明细">
      <table className="lib-table cs-table">
        <thead>
          <tr><th>时间</th><th>节点</th><th>模型</th><th className="is-num">Token</th><th className="is-num">费用</th><th className="is-num">耗时</th><th>状态</th><th>场景</th></tr>
        </thead>
        <tbody>
          {topCalls.map((c) => {
            const status = accountingStatusMeta(c.accounting_status);
            return (
              <tr key={c.llm_call_id}>
                <td className="is-nowrap">{fmtTime(c.created_at)}</td>
                <td><NodeName nodeId={c.node_id} /></td>
                <td className="is-nowrap">{c.model || "—"}{c.provider ? <span className="cs-sub"> · {c.provider}</span> : null}</td>
                <td className="is-num">{fmtInt(c.total_tokens)}</td>
                <td className="is-num">{fmtMoney(c.cost, c.currency || currency)}</td>
                <td className="is-num">{c.latency_ms === null || c.latency_ms === undefined ? "—" : `${(Number(c.latency_ms) / 1000).toFixed(1)} 秒`}</td>
                <td>
                  {c.error_code
                    ? <Tag tone="danger" title={c.error_code}>失败</Tag>
                    : <Tag tone={status.tone} title={c.accounting_status || undefined}>{status.label}</Tag>}
                </td>
                <td>
                  {c.scene_id
                    ? <button type="button" className="btn btn-quiet btn-sm cs-scene-link" disabled={loading} title={c.scene_id} onClick={() => onDrillScene(c.scene_id)}>{sceneLabelById(chapters, c.scene_id)}</button>
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </TableScroller>
  );
}

const QUOTA_METER_KEYS = [
  "daily_tokens",
  "monthly_tokens",
  "project_daily_tokens",
  "daily_requests",
  "concurrent_requests",
  "daily_cost_usd",
];

// 只认 limit：后端仅在闸门关闭时把 limit 置 null。不要拿 enforced 当判据——
// 缺该字段的载荷（旧后端、下钻时保留的旧 quota）会被判成「未设限」，在闸门
// 其实已启用时谎报无上限。安全展示必须朝「有上限」的方向失败。
export function quotaArmed(meter) {
  return !!meter && meter.limit !== null && meter.limit !== undefined;
}

function QuotaRow({ label, meter, suffix = "token", decimals = 0, armedOnly = false }) {
  if (!meter) return null;
  const armed = quotaArmed(meter);
  if (armedOnly && !armed) return null;
  const used = Number(meter.used || 0);
  const fmt = (v) => (decimals > 0 ? Number(v).toFixed(decimals) : Number(v).toLocaleString());
  // 额度关闭时仍然报用量，只是没有可填的进度条。
  if (!armed) {
    return (
      <div className="cs-quota-row">
        <span>{label}</span>
        <span className="cs-bar-val">{fmt(used)} {suffix} · 未设限</span>
      </div>
    );
  }
  const limit = Number(meter.limit);
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
  return (
    <div className="cs-quota-item">
      <div className="cs-quota-row">
        <span>{label}</span>
        <span className={`cs-bar-val${ratio >= 0.9 ? " is-danger" : ""}`}>
          {fmt(used)} / {fmt(limit)} {suffix} · {Math.round(ratio * 100)}%
        </span>
      </div>
      <div role="progressbar" aria-label={label} aria-valuemin="0" aria-valuemax={limit} aria-valuenow={used}>
        <MeterBar ratio={ratio} danger={ratio >= 0.9} />
      </div>
    </div>
  );
}

export function QuotaSection({ quota }) {
  if (!quota) return null;
  // 同样不信任 any_enforced：由各 meter 自行推导，缺字段时判为「已武装」。
  const armed = QUOTA_METER_KEYS.some((key) => quotaArmed(quota[key]));
  const costArmed = quotaArmed(quota.daily_cost_usd);
  const tz = quota.period_timezone || "UTC";
  return (
    <section className="card cs-card" aria-label={armed ? "全局模型额度" : "全局模型用量"}>
      <h3 className="cs-card-title">
        <I.ShieldCheck size={14} /> {armed ? "全局硬额度" : "全局用量"}
      </h3>
      <QuotaRow label="今日总量" meter={quota.daily_tokens} />
      <QuotaRow label="本月总量" meter={quota.monthly_tokens} />
      <QuotaRow label="本项目今日" meter={quota.project_daily_tokens} />
      <QuotaRow label="今日请求" meter={quota.daily_requests} suffix="次" />
      <QuotaRow label="并发请求" meter={quota.concurrent_requests} suffix="路" />
      {/* 金额只在闸门启用时出现：它按 env 单价计价，与本页其余 pricing.yaml 口径
          不同，未启用时恒为 0，摆出来会和上方「全书累计成本」自相矛盾。 */}
      <QuotaRow label="今日金额" meter={quota.daily_cost_usd} suffix="USD" decimals={4} armedOnly />
      <p className="cs-note">
        {armed
          ? `额度按 ${tz} 结算；达到上限会在请求发出前拒绝，不产生模型费用。`
          : `按 ${tz} 结算的用量读数。当前未设任何硬额度，生成不会被额度拦下。`}
        {armed && !costArmed ? " 金额上限未启用。" : ""}
      </p>
      {(!armed || !costArmed) && (
        <details className="cs-details">
          <summary>怎么设上限</summary>
          <p className="cs-note">
            {armed
              ? "金额上限需要同时设置 NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD 与模型单价，然后重启后端。"
              : "在后端设置对应的额度环境变量（如 NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT、NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS）并重启后端。"}
          </p>
        </details>
      )}
    </section>
  );
}

/* 口径说明（默认收起）：价格来源、三口径、尝试可观测性 */
export function CalibersDetails({ summary }) {
  const cal = summary && summary.calibers;
  const obs = summary && summary.attempt_observability;
  const CAL_LABEL = { estimate: "估算口径", provider_actual: "供应商实际", budget_charged: "预算计费" };
  return (
    <details className="card cs-card cs-details">
      <summary>口径说明</summary>
      <div className="cs-details-body">
        <p className="cs-note">
          价格取自 config/pricing.yaml 的单价快照；标「估算」的是占位单价，接入真实计费后替换单价即可。
        </p>
        {summary && summary.cross_provider && (
          <p className="cs-note is-warn">
            用了不止一家模型供应商：各家分词器不同，token 不能直接相加，本页合计以费用为准。
            {summary.tokens_by_provider && (
              <> 分列：{Object.entries(summary.tokens_by_provider).map(([p, t]) => `${p} ${fmtInt(t)}`).join("、")}</>
            )}
          </p>
        )}
        {cal && (
          <div className="cs-kv">
            {Object.entries(CAL_LABEL).map(([k, label]) => cal[k] && (
              <div key={k} className="cs-quota-row">
                <span>{label}</span>
                <span className="cs-bar-val" title={cal[k].source}>{fmtInt(cal[k].tokens)} token</span>
              </div>
            ))}
          </div>
        )}
        {obs && (
          <div className="cs-kv">
            <div className="cs-quota-row"><span>实际发出的请求</span><span className="cs-bar-val">{fmtInt(obs.physical_attempt_count)} 次</span></div>
            <div className="cs-quota-row"><span>重试</span><span className="cs-bar-val" title={`传输 ${fmtInt(obs.transport_retry_attempt_count)} / 解析 ${fmtInt(obs.response_parse_retry_attempt_count)}`}>{fmtInt(obs.retry_attempt_count)} 次</span></div>
            <div className="cs-quota-row"><span>降级</span><span className="cs-bar-val">{fmtInt(obs.degrade_attempt_count)} 次</span></div>
            <div className="cs-quota-row"><span>异常</span><span className="cs-bar-val">{fmtInt(obs.exception_count)} 次</span></div>
            <div className="cs-quota-row"><span>用量为估算</span><span className="cs-bar-val">{fmtInt(obs.usage_estimate_count)} 次</span></div>
            {obs.legacy_parent_without_attempt_count > 0 && (
              <p className="cs-note is-warn">
                含 {fmtInt(obs.legacy_parent_without_attempt_count)} 条旧账（{fmtInt(obs.legacy_unreconstructable_tokens)} token 无法重构到单次尝试）。
              </p>
            )}
          </div>
        )}
      </div>
    </details>
  );
}

/* ---- 下钻面板（章节 / 场景） ---- */
export function DrillPanel({ st, chapters }) {
  const s = st.summary;
  if (!s) return null;
  const isScene = st.level === "scene";
  const budget = s.budget;
  const extra = s.extra_cost;
  const armedBudget = isScene && budget && budget.budget !== null && budget.budget !== undefined;
  const multiplier = armedBudget && budget.baseline ? Math.round(Number(budget.budget) / Number(budget.baseline)) : null;
  const title = isScene ? sceneLabelById(chapters, s.scene_id, { withTitle: true }) : chapterLabelById(chapters, s.chapter_id);
  return (
    <section className="cs-grid">
      <div className="cs-drill-head">
        <button type="button" className="btn btn-ghost btn-sm" onClick={costBack}>
          <I.ChevronLeft size={13} /> 返回全书
        </button>
        <h2 className="cs-drill-title" title={isScene ? s.scene_id : s.chapter_id}>{title}<span className="cs-sub"> 的成本</span></h2>
        <EstimatePill on={s.is_estimate} />
      </div>

      <div className="q-stats">
        <StatCard label="总成本" value={fmtMoney(s.total_cost, s.currency)} />
        <StatCard label="Token" value={fmtInt(s.total_tokens)} hint={s.cross_provider ? "跨供应商，以费用为准" : null} />
        <StatCard label="调用数" value={fmtInt(s.call_count)} />
        {!isScene && <StatCard label="已归档场景" value={fmtInt(s.archived_scene_count)} hint={s.tokens_per_archived_scene ? `场均 ${fmtInt(s.tokens_per_archived_scene)} token` : null} />}
        {armedBudget && (
          <StatCard label={multiplier ? `场景预算（${multiplier} 倍基线）` : "场景预算"} value={`${fmtInt(budget.used)} / ${fmtInt(budget.budget)}`}
                    tone={budget.over_budget ? "danger" : undefined}
                    hint={budget.over_budget ? "已超预算" : `使用率 ${fmtPct(budget.usage_ratio)}`} />
        )}
        {isScene && budget && budget.disarmed && (
          <StatCard label="场景预算" value="不限" hint={`已用 ${fmtInt(budget.used)} token`} />
        )}
      </div>

      {armedBudget && (
        <div role="progressbar" aria-label="场景 token 预算使用率" aria-valuemin="0" aria-valuemax={budget.budget} aria-valuenow={budget.used}>
          <MeterBar ratio={budget.usage_ratio} danger={!!budget.over_budget || (budget.usage_ratio || 0) >= 0.9} />
        </div>
      )}

      <div className="card cs-card">
        <h3 className="cs-card-title">各阶段占比</h3>
        <PhaseBars breakdown={s.phase_breakdown} currency={s.currency} />
      </div>

      {extra && (
        <p className="cs-note">
          额外成本占总成本 {fmtPct(extra.retry_cost_ratio)}：失败重试 {fmtMoney(extra.failed_call_cost, s.currency)}、
          重复质检 {fmtMoney(extra.repeat_qc_cost, s.currency)}、补候选 {fmtMoney(extra.low_dispersion_topup_cost, s.currency)}。
        </p>
      )}

      <CalibersDetails summary={s} />
    </section>
  );
}
