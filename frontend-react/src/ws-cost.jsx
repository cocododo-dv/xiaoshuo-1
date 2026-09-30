import React from "react";
import { I } from "./icons.jsx";
import { StatCard } from "./ws-quality-ui.jsx";
import { useCatalogChapters } from "./ws-catalog.jsx";
import { WsWorks, useActiveWorkIdentity } from "./ws-works.jsx";
import { EmptyState, Notice, PageHeader, Segmented, Spinner } from "./ws-ui.jsx";
import { COST_WINDOWS, costLoad, csSnapshot, useCostState } from "./ws-cost-store.js";
import {
  CalibersDetails, ChapterTable, DrillPanel, EstimatePill, ModelTable, NodeBars, PhaseBars, QuotaSection, TopCallsTable,
  TrendChart, costText, fmtInt, pricingComplete,
} from "./ws-cost-parts.jsx";
import { readyWorkId } from "./lib/ready-work.js";
import { isRealWorkId } from "./lib/work-id.js";

/* ==========================================================
   WsCost — 成本看板
   数据源（均只读）：
     GET /api/v2/projects/{id}/cost-dashboard?days=N
         —— 项目级一读聚合：summary + 逐日趋势 + 模型/节点/章节
            构成 + Top 调用 + 全局额度快照
     GET /api/v2/projects/{id}/cost-summary?chapter_id= / ?scene_id=
         —— 章节 / 场景下钻
   口径：以 token 为主（2026-09-30 批准 #4）——卡片、图、表都先说 token；费用只算 config/pricing.yaml
   里写了单价的模型，没写的显示「未定价」（后端 cost 为 null），不再用占位估算价编一个数。跨 provider 的
   token 分词器不同，合计只作参考；三口径 estimate / provider_actual / budget_charged。
   跟随当前作品：视图挂载/切换作品时自动加载，无需手输项目 ID。

   这个文件只管页面本身：跟随当前作品加载、窗口切换、下钻与返回。store 在 ws-cost-store.js，
   图表 / 表格 / 额度 / 下钻面板这些展示件在 ws-cost-parts.jsx。
   ========================================================== */

function WsCost() {
  const st = useCostState();
  /* 只跟「当前是哪部作品」走：写作时的字数回写不让整张看板重渲。发请求用能拿去发请求的那个 id
     （新建的作品还没拿到正式 id 时是 null，见 lib/ready-work）——它变的时候身份快照也变，这里跟着重渲 */
  const identity = useActiveWorkIdentity();
  const activeId = readyWorkId(WsWorks);
  const chapters = useCatalogChapters() || [];

  // 跟随当前作品：挂载 / 切书自动加载；已有同项目缓存则不重复请求
  React.useEffect(() => {
    if (!activeId) return;
    const cur = csSnapshot();
    if (cur.projectId !== activeId || (!cur.dashboard && !cur.loading)) costLoad(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  /* 只摆当前作品的账（复核 Q5-R2）：store 在模块里，视图重挂后它还留着上一部作品的看板——新建的作品在等正式 id 时
     这里不发请求也不清，一直留到正式 id 回来；切到另一部作品、effect 还没发请求的那一帧也是。这些时候都当「还没读到」：
     不把上一部的数摆在这部名下，刷新 / 统计窗口 / 下钻也不会去动上一部的账。 */
  const mine = !!activeId && st.projectId === activeId;
  const level = mine ? st.level : "project";
  const dash = mine ? st.dashboard : null;
  const loading = mine && st.loading;
  const error = mine ? st.error : null;
  /* 选中了一部作品、它的账还没开始读：新建的作品在等正式 id，或者刚切过来、请求还没发出去 */
  const waiting = activeId ? !mine : isRealWorkId(identity && identity.id);
  const s = level === "project" ? (dash && dash.summary) : null;
  const currency = (s && s.currency) || (st.summary && st.summary.currency) || "USD";
  const complete = pricingComplete(s);
  const projectId = activeId;
  const empty = s && !s.call_count;
  const reload = () => costLoad(projectId, level === "project" ? { days: st.days } : undefined);
  const unpricedCalls = (s && s.pricing && Number(s.pricing.unpriced_call_count)) || 0;

  return (
    <div className="ws-page ws-view q-cost" data-screen-label="cost">
      <PageHeader
        title="成本看板"
        description="每一次生成、重试、审读与质检都记在账上：这里看 token 花在哪、趋势如何、用量走到了哪一步。"
        actions={(
          <>
            {level === "project" && (
              <Segmented
                label="统计窗口"
                value={st.days}
                onChange={(n) => costLoad(projectId, { days: n })}
                options={COST_WINDOWS.map((n) => ({ value: n, label: `近 ${n} 天`, disabled: loading || !projectId }))}
              />
            )}
            <button type="button" className="btn btn-ghost" disabled={loading || !projectId} onClick={reload}>
              {loading ? <Spinner size={14} /> : <I.Refresh size={14} />} {loading ? "读取中…" : "刷新"}
            </button>
          </>
        )}
      />

      {error && (
        <Notice tone="danger" title="成本没有加载出来" className="q-notice"
          actions={<button type="button" className="btn btn-ghost btn-sm" onClick={reload}>重试</button>}>
          {error}
        </Notice>
      )}

      {!projectId && !waiting && (
        <EmptyState compact className="q-empty" title="还没有选作品">先在书架选一部作品，成本看板会自动跟随当前作品。</EmptyState>
      )}

      {mine && level !== "project" && <DrillPanel st={st} chapters={chapters} />}

      {level === "project" && dash && (
        <div className="cs-grid">
          {empty ? (
            <EmptyState compact className="q-empty" title="这部作品还没有任何模型调用">生成一次草稿或跑一次质检后，这里会出现成本账。</EmptyState>
          ) : (
            <>
              {/* 总览卡片：token 在前，费用只在真算得出时给数 */}
              <div className="q-stats">
                <StatCard label="全书累计 Token" value={fmtInt(s.total_tokens)}
                          hint={s.cross_provider ? "跨供应商，合计只作参考" : null}>
                  <EstimatePill on={s.is_estimate} />
                </StatCard>
                <StatCard label={`近 ${st.days} 天 Token`} value={fmtInt(dash.trend && dash.trend.window_tokens)}
                          hint={dash.trend ? `${fmtInt(dash.trend.window_call_count)} 次调用` : null} />
                <StatCard label="累计调用" value={fmtInt(s.call_count)} />
                <StatCard label="归档场均 Token" value={s.tokens_per_archived_scene === null || s.tokens_per_archived_scene === undefined ? "—" : fmtInt(s.tokens_per_archived_scene)}
                          hint={`已归档 ${fmtInt(s.archived_scene_count)} / ${fmtInt(s.scene_count)} 场`} />
                <StatCard label="全书累计费用" value={costText(s.total_cost, currency, complete)}
                          hint={unpricedCalls > 0 ? `${fmtInt(unpricedCalls)} 次调用的模型没有单价` : null} />
                <StatCard label="归档章均费用"
                          value={!Number(s.archived_chapter_count) ? "—" : costText(s.cost_per_archived_chapter, currency, complete)}
                          hint={`已归档 ${fmtInt(s.archived_chapter_count)} / ${fmtInt(s.chapter_count)} 章`} />
              </div>

              {/* 趋势 */}
              <section className="card cs-card">
                <h3 className="cs-card-title"><I.Activity size={14} /> 逐日用量<span className="cs-sub">近 {st.days} 天，按 UTC 计日</span></h3>
                <TrendChart trend={dash.trend} currency={currency} complete={complete} />
              </section>

              {/* 构成：阶段 | 节点 */}
              <div className="cs-split">
                <section className="card cs-card">
                  <h3 className="cs-card-title">各阶段占比</h3>
                  <PhaseBars breakdown={s.phase_breakdown} currency={currency} complete={complete} />
                </section>
                <section className="card cs-card">
                  <h3 className="cs-card-title">用量最多的节点</h3>
                  <NodeBars byNode={dash.by_node} currency={currency} complete={complete} />
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
                <ChapterTable byChapter={dash.by_chapter} chapters={chapters} currency={currency} complete={complete} loading={loading}
                              onDrill={(cid) => costLoad(projectId, { chapterId: cid })} />
              </section>

              {/* Top 调用明细 */}
              <section className="card cs-card">
                <h3 className="cs-card-title">用量最多的调用</h3>
                <TopCallsTable topCalls={dash.top_calls} chapters={chapters} currency={currency} loading={loading}
                               onDrillScene={(sid) => costLoad(projectId, { sceneId: sid })} />
              </section>

              <CalibersDetails summary={s} />
            </>
          )}

          <QuotaSection quota={st.quota} />
        </div>
      )}

      {level === "project" && !dash && (loading || waiting) && (
        <div className="cs-loading" role="status"><Spinner size={14} /> 正在读取账本…</div>
      )}
    </div>
  );
}

export { WsCost };
