import React from "react";
import { I } from "./icons.jsx";
import { StatCard } from "./ws-quality-ui.jsx";
import { useCatalogChapters } from "./ws-catalog.jsx";
import { useActiveWorkIdentity } from "./ws-works.jsx";
import { EmptyState, Notice, PageHeader, Segmented, Spinner } from "./ws-ui.jsx";
import { COST_WINDOWS, PHASE_LABEL, costBack, costLoad, csSnapshot, useCostState } from "./ws-cost-store.js";
import {
  CalibersDetails, ChapterTable, DrillPanel, EstimatePill, ModelTable, NodeBars, PhaseBars, QuotaSection, TopCallsTable,
  TrendChart, fmtInt, fmtMoney, quotaArmed,
} from "./ws-cost-parts.jsx";

/* ==========================================================
   WsCost — 成本看板
   数据源（均只读）：
     GET /api/v2/projects/{id}/cost-dashboard?days=N
         —— 项目级一读聚合：summary + 逐日趋势 + 模型/节点/章节
            构成 + Top 调用 + 全局额度快照
     GET /api/v2/projects/{id}/cost-summary?chapter_id= / ?scene_id=
         —— 章节 / 场景下钻
   口径：跨 provider 的 token 不直接相加（分词器不同，汇总以费用为准）；
   三口径 estimate / provider_actual / budget_charged；价格来自
   config/pricing.yaml 快照，占位价全部 is_estimate。
   跟随当前作品：视图挂载/切换作品时自动加载，无需手输项目 ID。

   这个文件只管页面本身：跟随当前作品加载、窗口切换、下钻与返回。store 在 ws-cost-store.js，
   图表 / 表格 / 额度 / 下钻面板这些展示件在 ws-cost-parts.jsx。
   ========================================================== */

function WsCost() {
  const st = useCostState();
  /* 只跟「当前是哪部作品」走：写作时的字数回写不让整张看板重渲 */
  const identity = useActiveWorkIdentity();
  const activeId = identity && identity.id && identity.id !== "__loading__" ? identity.id : null;
  const chapters = useCatalogChapters() || [];

  // 跟随当前作品：挂载 / 切书自动加载；已有同项目缓存则不重复请求
  React.useEffect(() => {
    if (!activeId) return;
    const cur = csSnapshot();
    if (cur.projectId !== activeId || (!cur.dashboard && !cur.loading)) costLoad(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const dash = st.dashboard;
  const s = st.level === "project" ? (dash && dash.summary) : null;
  const currency = (s && s.currency) || (st.summary && st.summary.currency) || "USD";
  const projectId = st.projectId || activeId;
  const empty = s && !s.call_count;
  const reload = () => costLoad(projectId, st.level === "project" ? { days: st.days } : undefined);

  return (
    <div className="ws-page ws-view q-cost" data-screen-label="cost">
      <PageHeader
        title="成本看板"
        description="每一次生成、重试、审读与质检都记在账上：这里看钱花在哪、趋势如何、用量走到了哪一步。"
        actions={(
          <>
            {st.level === "project" && (
              <Segmented
                label="统计窗口"
                value={st.days}
                onChange={(n) => costLoad(projectId, { days: n })}
                options={COST_WINDOWS.map((n) => ({ value: n, label: `近 ${n} 天`, disabled: st.loading || !projectId }))}
              />
            )}
            <button type="button" className="btn btn-ghost" disabled={st.loading || !projectId} onClick={reload}>
              {st.loading ? <Spinner size={14} /> : <I.Refresh size={14} />} {st.loading ? "读取中…" : "刷新"}
            </button>
          </>
        )}
      />

      {st.error && (
        <Notice tone="danger" title="成本没有加载出来" className="q-notice"
          actions={projectId ? <button type="button" className="btn btn-ghost btn-sm" onClick={reload}>重试</button> : null}>
          {st.error}
        </Notice>
      )}

      {!projectId && !st.loading && (
        <EmptyState compact className="q-empty" title="还没有选作品">先在书架选一部作品，成本看板会自动跟随当前作品。</EmptyState>
      )}

      {projectId && st.level !== "project" && <DrillPanel st={st} chapters={chapters} />}

      {projectId && st.level === "project" && dash && (
        <div className="cs-grid">
          {empty ? (
            <EmptyState compact className="q-empty" title="这部作品还没有任何模型调用">生成一次草稿或跑一次质检后，这里会出现成本账。</EmptyState>
          ) : (
            <>
              {/* 总览卡片 */}
              <div className="q-stats">
                <StatCard label="全书累计成本" value={fmtMoney(s.total_cost, currency)}>
                  <EstimatePill on={s.is_estimate} />
                </StatCard>
                <StatCard label={`近 ${st.days} 天成本`} value={fmtMoney(dash.trend && dash.trend.window_cost, currency)}
                          hint={dash.trend ? `${fmtInt(dash.trend.window_call_count)} 次调用` : null} />
                <StatCard label="累计 Token" value={fmtInt(s.total_tokens)}
                          hint={s.cross_provider ? "跨供应商，以费用为准" : null} />
                <StatCard label="累计调用" value={fmtInt(s.call_count)} />
                <StatCard label="归档章均成本" value={s.cost_per_archived_chapter === null || s.cost_per_archived_chapter === undefined ? "—" : fmtMoney(s.cost_per_archived_chapter, currency)}
                          hint={`已归档 ${fmtInt(s.archived_chapter_count)} / ${fmtInt(s.chapter_count)} 章`} />
                <StatCard label="归档场均 Token" value={s.tokens_per_archived_scene === null || s.tokens_per_archived_scene === undefined ? "—" : fmtInt(s.tokens_per_archived_scene)}
                          hint={`已归档 ${fmtInt(s.archived_scene_count)} / ${fmtInt(s.scene_count)} 场`} />
              </div>

              {/* 趋势 */}
              <section className="card cs-card">
                <h3 className="cs-card-title"><I.Activity size={14} /> 逐日成本<span className="cs-sub">近 {st.days} 天，按 UTC 计日</span></h3>
                <TrendChart trend={dash.trend} currency={currency} />
              </section>

              {/* 构成：阶段 | 节点 */}
              <div className="cs-split">
                <section className="card cs-card">
                  <h3 className="cs-card-title">各阶段占比</h3>
                  <PhaseBars breakdown={s.phase_breakdown} currency={currency} />
                </section>
                <section className="card cs-card">
                  <h3 className="cs-card-title">成本最高的节点</h3>
                  <NodeBars byNode={dash.by_node} currency={currency} />
                </section>
              </div>

              {/* 模型构成 */}
              <section className="card cs-card">
                <h3 className="cs-card-title">按模型</h3>
                <ModelTable byModel={dash.by_model} currency={currency} />
              </section>

              {/* 章节构成（可下钻） */}
              <section className="card cs-card">
                <h3 className="cs-card-title">按章节</h3>
                <ChapterTable byChapter={dash.by_chapter} chapters={chapters} currency={currency} loading={st.loading}
                              onDrill={(cid) => costLoad(projectId, { chapterId: cid })} />
              </section>

              {/* Top 调用明细 */}
              <section className="card cs-card">
                <h3 className="cs-card-title">费用最高的调用</h3>
                <TopCallsTable topCalls={dash.top_calls} chapters={chapters} currency={currency} loading={st.loading}
                               onDrillScene={(sid) => costLoad(projectId, { sceneId: sid })} />
              </section>

              <CalibersDetails summary={s} />
            </>
          )}

          <QuotaSection quota={st.quota} />
        </div>
      )}

      {projectId && st.level === "project" && !dash && st.loading && (
        <div className="cs-loading" role="status"><Spinner size={14} /> 正在读取账本…</div>
      )}
    </div>
  );
}

/* store 与额度件原先就从这里导出（单测这样 import），拆文件后照样从这里拿得到 */
export { WsCost, costLoad, costBack, csSnapshot, useCostState, PHASE_LABEL, QuotaSection, quotaArmed };
