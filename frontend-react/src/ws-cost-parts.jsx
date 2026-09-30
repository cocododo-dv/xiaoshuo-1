import React from "react";
import { I } from "./icons.jsx";
import { ProgressBar, Spinner, StatTile, Tag } from "./ws-ui.jsx";
import { chapterLabelById, sceneLabelById } from "./labels/catalog.js";
import { accountingStatusMeta, llmNodeLabel } from "./labels/llm.js";
import { PHASE_LABEL, costBack } from "./ws-cost-store.js";
import { formatIntOrDash as fmtInt, formatPercentRounded as fmtPct, isoMonthDay as fmtDay, formatLocalMonthDayTime as fmtTime } from "./lib/format.js";

/* ==========================================================
   成本看板的展示件（从 ws-cost.jsx 拆出，2026-09-22）
   格式化、统计卡、趋势柱状图、阶段 / 节点 / 模型 / 章节 / 调用明细、全局用量、口径说明、下钻面板。
   只画不取数：数据都由 ws-cost.jsx 从 store 快照里传进来。
   以 token 为主（2026-09-30 批准 #4）：图、条、表都按 token 量；金额只在后端真算出来时（cost 不是 null）才出现——
   后端只给 config/pricing.yaml 里写了单价的模型算钱，其余是「未定价」，不再拿占位估算价编一个数。
   ========================================================== */

/* ---- 格式化 ---- */
/* 金额统一两位小数；不足 0.01 的非零小额才给四位（否则显示成 0.00）。 */
export function fmtMoney(v, cur) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const digits = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2;
  return `${n.toFixed(digits)} ${cur || "USD"}`;
}
/* 数字 / 时间文案住在 lib/format.js（成本看板的口径各有名字）；fmtInt 照旧从这里转出给 ws-cost.jsx */
export { fmtInt };

export const UNPRICED = "未定价";

/* 一组调用的费用：一条定了价的都没有（cost 是 null）→「未定价」；全都定了价 → 金额；
   只有一部分定了价（complete 为假）→「已定价部分 X」——后端的金额只含定了价的那部分，不能当成全部。
   行上没有自己的 priced 标记（阶段 / 节点 / 章节 / 逐日）时，complete 取这一级汇总的 pricing.complete。 */
export function costText(cost, currency, complete = true) {
  if (cost === null || cost === undefined) return UNPRICED;
  return complete ? fmtMoney(cost, currency) : `已定价部分 ${fmtMoney(cost, currency)}`;
}

/* 条形图的附注：有金额才补一句（没定价就不在每一条上重复「未定价」，费用卡与口径说明里说一次） */
function costTail(cost, currency, complete) {
  return cost === null || cost === undefined ? "" : ` · ${costText(cost, currency, complete)}`;
}

/* 这一级汇总是否全部定了价（缺 pricing 的旧载荷按「全部定了价」处理：那时金额总有值） */
export function pricingComplete(summary) {
  const pricing = summary && summary.pricing;
  return !pricing || pricing.complete !== false;
}

/* ==========================================================
   小部件
   ========================================================== */

/* 统计卡：ws-ui 的 StatTile 外加一张卡的底（.q-stats / .q-stat 借用 ws-quality.css，与文学质量的统计块同一套）。
   children 是跟在注释前面的小标签（如 EstimatePill 的「用量含估算」）。原在 ws-quality-ui.jsx——文学质量早就不用它了，
   只有成本看板用，2026-10 搬到这里（F04-14 / F05-20）。 */
export function StatCard({ label, value, hint, tone, children, className }) {
  const note = children || hint
    ? <>{children}{children && hint ? " " : null}{hint}</>
    : null;
  return <StatTile className={`q-stat${className ? ` ${className}` : ""}`} label={label} value={value} hint={note} tone={tone} />;
}

/* 有些调用的 token 数是估算的（供应商没回报实际用量，按字数估）：标一下。价格不再有占位估算。 */
export function EstimatePill({ on }) {
  if (!on) return null;
  return <Tag tone="warn" dot title="有些调用的供应商没有回报实际用量，这些调用的 token 数是按字数估算的">用量含估算</Tag>;
}

/* 模型节点：有中文名给中文名，没有就等宽显示原始 id（机器标识）；原始 id 总在 title 里 */
function NodeName({ nodeId }) {
  const label = llmNodeLabel(nodeId);
  if (!nodeId) return <span>—</span>;
  return label
    ? <span className="cs-node" title={nodeId}>{label}</span>
    : <code className="cs-node is-raw" title={nodeId}>{nodeId}</code>;
}

/* 逐日 token 柱状图：后端稠密补零序列，纯 SVG，无第三方依赖 */
export function TrendChart({ trend, currency, complete = true }) {
  const series = (trend && trend.series) || [];
  if (!series.length) return null;
  const W = 720, H = 110, PADX = 2;
  const slot = (W - PADX * 2) / series.length;
  const bw = Math.max(2, slot - 2);
  const maxTokens = Math.max(...series.map((d) => d.tokens || 0));
  const scale = maxTokens > 0 ? (H - 12) / maxTokens : 0;
  const activeDays = series.filter((d) => (d.call_count || 0) > 0).length;
  return (
    <div>
      <svg className="cs-trend" viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none"
           role="img" aria-label={`近 ${series.length} 天逐日 token 用量柱状图`}>
        {series.map((d, i) => {
          const h = (d.tokens || 0) > 0 ? Math.max(2, d.tokens * scale) : 1;
          return (
            <rect key={d.date} x={PADX + i * slot} y={H - h} width={bw} height={h} rx={1}
                  className={(d.call_count || 0) > 0 ? "cs-bar is-on" : "cs-bar"}>
              <title>{`${d.date} · ${fmtInt(d.tokens || 0)} token（${fmtInt(d.call_count)} 次）${costTail(d.cost, currency, complete)}`}</title>
            </rect>
          );
        })}
      </svg>
      <div className="cs-trend-axis">
        <span>{fmtDay(series[0].date)}</span>
        <span>
          {activeDays === 0
            ? "窗口内暂无调用"
            : `合计 ${fmtInt(trend.window_tokens)} token · ${fmtInt(trend.window_call_count)} 次调用${costTail(trend.window_cost, currency, complete)}`}
        </span>
        <span>{fmtDay(series[series.length - 1].date)}</span>
      </div>
    </div>
  );
}

/* 比例条（0–1）：ws-ui 的 ProgressBar（role=progressbar，读屏报名字与百分比），轨道尺寸沿用 .cs-meter；
   填充色走语气：平常 ok，超预算 / 快满了 danger */
function MeterBar({ ratio, label, danger = false }) {
  const pct = Math.round(Math.min(1, Math.max(0, ratio || 0)) * 100);
  return <ProgressBar value={pct} label={label} tone={danger ? "danger" : "ok"} className="cs-meter" />;
}

export function PhaseBars({ breakdown, currency, complete = true }) {
  const rows = Object.keys(PHASE_LABEL)
    .map((k) => ({ k, tokens: 0, cost: null, share: 0, call_count: 0, ...((breakdown || {})[k] || {}) }))
    .filter((r) => (r.call_count || 0) > 0);
  if (!rows.length) return <p className="cs-none">暂无阶段用量。</p>;
  return (
    <div className="cs-bars">
      {rows.map((r) => (
        <div key={r.k}>
          <div className="cs-bar-row" title={`${fmtInt(r.call_count)} 次调用`}>
            <span>{PHASE_LABEL[r.k]}</span>
            <span className="cs-bar-val">{fmtInt(r.tokens)} token · {fmtPct(r.share)}{costTail(r.cost, currency, complete)}</span>
          </div>
          <MeterBar ratio={r.share} label={`${PHASE_LABEL[r.k]}占比`} />
        </div>
      ))}
    </div>
  );
}

function TableScroller({ label, children }) {
  return <div className="cs-table-scroll" role="region" aria-label={label} tabIndex={0}>{children}</div>;
}

/* 表格里的费用格：没定价给一个中性标签，一眼能和金额分开 */
function CostCell({ cost, currency, complete = true }) {
  if (cost === null || cost === undefined) return <Tag tone="neutral">{UNPRICED}</Tag>;
  return costText(cost, currency, complete);
}

export function ModelTable({ byModel, currency }) {
  if (!byModel || !byModel.length) return <p className="cs-none">暂无模型调用。</p>;
  return (
    <TableScroller label="按模型的用量构成">
      <table className="lib-table cs-table">
        <thead>
          <tr><th>提供商</th><th>模型</th><th className="is-num">Token</th><th className="is-num">调用</th><th className="is-num">费用</th><th>用量</th></tr>
        </thead>
        <tbody>
          {byModel.map((m) => (
            <tr key={`${m.provider}:${m.model}`}>
              <td>{m.provider}</td>
              <td>{m.model}</td>
              <td className="is-num">{fmtInt(m.tokens)}</td>
              <td className="is-num">{fmtInt(m.call_count)}</td>
              <td className="is-num"><CostCell cost={m.cost} currency={currency} complete={m.priced !== false} /></td>
              <td>{m.is_estimate ? <Tag tone="warn" title="有些调用的 token 数是按字数估算的">含估算</Tag> : <Tag tone="info">实际</Tag>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroller>
  );
}

export function NodeBars({ byNode, currency, complete = true }) {
  const top = (byNode && byNode.top) || [];
  if (!top.length) return <p className="cs-none">暂无节点用量。</p>;
  const max = Math.max(...top.map((n) => n.tokens || 0), 1);
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
            <span className="cs-bar-val">{fmtInt(n.tokens)} token · {fmtInt(n.call_count)} 次{costTail(n.cost, currency, complete)}</span>
          </div>
          <MeterBar ratio={(n.tokens || 0) / max} label={`${llmNodeLabel(n.node_id) || n.node_id}的用量`} />
        </div>
      ))}
      {remainder && (
        <p className="cs-none">
          其余 {fmtInt(remainder.node_count)} 个节点合计 {fmtInt(remainder.tokens)} token（{fmtInt(remainder.call_count)} 次）{costTail(remainder.cost, currency, complete)}
        </p>
      )}
    </div>
  );
}

export function ChapterTable({ byChapter, chapters, currency, complete = true, onDrill, loading }) {
  if (!byChapter || !byChapter.length) return <p className="cs-none">暂无章节用量——生成过草稿的章节会出现在这里。</p>;
  return (
    <TableScroller label="按章节的用量构成">
      <table className="lib-table cs-table">
        <thead>
          <tr><th>章节</th><th className="is-num">Token</th><th className="is-num">调用</th><th className="is-num">涉及场景</th><th className="is-num">费用</th><th><span className="ws-sr-only">明细</span></th></tr>
        </thead>
        <tbody>
          {byChapter.map((c) => (
            <tr key={c.chapter_id || "__none__"}>
              <td title={c.chapter_id || undefined}>{chapterLabelById(chapters, c.chapter_id)}</td>
              <td className="is-num">{fmtInt(c.tokens)}</td>
              <td className="is-num">{fmtInt(c.call_count)}</td>
              <td className="is-num">{fmtInt(c.scene_count)}</td>
              <td className="is-num"><CostCell cost={c.cost} currency={currency} complete={complete} /></td>
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
    <TableScroller label="用 token 最多的调用明细">
      <table className="lib-table cs-table">
        <thead>
          <tr><th>时间</th><th>节点</th><th>模型</th><th className="is-num">Token</th><th className="is-num">费用</th><th className="is-num">耗时</th><th>状态</th><th>场景</th></tr>
        </thead>
        <tbody>
          {topCalls.map((c) => {
            const status = accountingStatusMeta(c.accounting_status);
            return (
              <tr key={c.llm_call_id}>
                <td className="is-nowrap" title={c.created_at || undefined}>{fmtTime(c.created_at) || "—"}</td>
                <td><NodeName nodeId={c.node_id} /></td>
                <td className="is-nowrap">{c.model || "—"}{c.provider ? <span className="cs-sub"> · {c.provider}</span> : null}</td>
                <td className="is-num">{fmtInt(c.total_tokens)}</td>
                <td className="is-num"><CostCell cost={c.priced === false ? null : c.cost} currency={c.currency || currency} /></td>
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

/* 全局用量：今日 / 本月 / 本作品今日的 token、今日请求、正在进行的调用（所有作品合计，本作品那一行除外）。
   2026-09-30 重评 R3（批准 #3a）：六道只能靠环境变量打开的全局额度闸删了，这里只剩读数——不拦任何生成；
   也不再有「今日金额」（它按另一套环境变量单价算，与本页的费用不是一把尺子）。 */
const USAGE_ROWS = [
  ["daily_tokens", "今日总量", "token"],
  ["monthly_tokens", "本月总量", "token"],
  ["project_daily_tokens", "本项目今日", "token"],
  ["daily_requests", "今日请求", "次"],
  ["concurrent_requests", "并发请求", "路"],
];

export function QuotaSection({ quota }) {
  if (!quota) return null;
  const rows = USAGE_ROWS.filter(([key]) => quota[key]);
  if (!rows.length) return null;
  const tz = quota.period_timezone || "UTC";
  return (
    <section className="card cs-card" aria-label="全局模型用量">
      <h3 className="cs-card-title"><I.Cpu size={14} /> 全局用量</h3>
      {rows.map(([key, label, suffix]) => (
        <div key={key} className="cs-quota-row">
          <span>{label}</span>
          <span className="cs-bar-val">{fmtInt(Number(quota[key].used || 0))} {suffix}</span>
        </div>
      ))}
      <p className="cs-note">所有作品合计（「本项目今日」只算这一部），今日 / 本月按 {tz} 计日。这里只是读数，不会拦下生成。</p>
    </section>
  );
}

/* 口径说明（默认收起）：费用怎么算、哪些模型还没有单价、三口径、尝试可观测性 */
export function CalibersDetails({ summary }) {
  const cal = summary && summary.calibers;
  const obs = summary && summary.attempt_observability;
  const unpriced = (summary && summary.pricing && summary.pricing.unpriced_models) || [];
  const CAL_LABEL = { estimate: "估算口径", provider_actual: "供应商实际", budget_charged: "预算计费" };
  return (
    <details className="card cs-card cs-details">
      <summary>口径说明</summary>
      <div className="cs-details-body">
        <p className="cs-note">
          这里以 token 为准。费用只算 config/pricing.yaml 里写了单价的模型；没有单价的显示「{UNPRICED}」，不按估算价折算。
        </p>
        {unpriced.length > 0 && (
          <p className="cs-note" data-testid="cs-unpriced-models">
            还没有单价的模型：{unpriced.map((m) => `${m.provider} / ${m.model}（${fmtInt(m.tokens)} token，${fmtInt(m.call_count)} 次）`).join("、")}。
          </p>
        )}
        {summary && summary.cross_provider && (
          <p className="cs-note is-warn">
            用了不止一家模型供应商：各家分词器不同，token 合计只作参考。
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

/* 白花的 token：发出去却失败了的请求。重评 R2 第 5 项删了「重复质检」「补候选」两项——一轮普通起草本来就依次跑
   几道不同的质检、几次生成类调用，它们每一场都被算成一笔并不存在的额外成本。没有失败就不说。 */
export function extraCostText(extra, currency) {
  const tokens = Number(extra && extra.failed_tokens) || 0;
  if (tokens <= 0) return "";
  const parts = [`占 ${fmtPct(extra.failed_share)}`];
  if (Number(extra.failed_attempt_count) > 0) parts.push(`${fmtInt(extra.failed_attempt_count)} 次失败的请求`);
  if (extra.failed_cost !== null && extra.failed_cost !== undefined) parts.push(fmtMoney(extra.failed_cost, currency));
  return `失败重试花掉 ${fmtInt(tokens)} token（${parts.join("，")}）。`;
}

/* ---- 下钻面板（章节 / 场景） ---- */
export function DrillPanel({ st, chapters }) {
  const s = st.summary;
  if (!s) {
    return st.loading ? <div className="cs-loading" role="status"><Spinner size={14} /> 正在读取账本…</div> : null;
  }
  const isScene = st.level === "scene";
  const currency = s.currency || "USD";
  const complete = pricingComplete(s);
  const budget = s.budget;
  const extra = extraCostText(s.extra_cost, currency);
  const armedBudget = isScene && budget && budget.budget !== null && budget.budget !== undefined;
  const multiplier = armedBudget && budget.baseline ? Math.round(Number(budget.budget) / Number(budget.baseline)) : null;
  const title = isScene ? sceneLabelById(chapters, s.scene_id, { withTitle: true }) : chapterLabelById(chapters, s.chapter_id);
  return (
    <section className="cs-grid">
      <div className="cs-drill-head">
        <button type="button" className="btn btn-ghost btn-sm" onClick={costBack}>
          <I.ChevronLeft size={13} /> 返回全书
        </button>
        <h2 className="cs-drill-title" title={isScene ? s.scene_id : s.chapter_id}>{title}<span className="cs-sub"> 的用量</span></h2>
        <EstimatePill on={s.is_estimate} />
      </div>

      <div className="q-stats">
        <StatCard label="Token" value={fmtInt(s.total_tokens)} hint={s.cross_provider ? "跨供应商，合计只作参考" : null} />
        <StatCard label="调用数" value={fmtInt(s.call_count)} />
        <StatCard label="费用" value={costText(s.total_cost, currency, complete)} />
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
        <MeterBar ratio={budget.usage_ratio} label="场景 token 预算使用率" danger={!!budget.over_budget || (budget.usage_ratio || 0) >= 0.9} />
      )}

      <div className="card cs-card">
        <h3 className="cs-card-title">各阶段占比</h3>
        <PhaseBars breakdown={s.phase_breakdown} currency={currency} complete={complete} />
      </div>

      {extra && <p className="cs-note" data-testid="cs-extra-cost">{extra}</p>}

      <CalibersDetails summary={s} />
    </section>
  );
}
