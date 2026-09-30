import React from "react";
import { I } from "./icons.jsx";
import { EmptyState, Spinner } from "./ws-ui.jsx";

/* ==========================================================
   章节编排 · 还没有一本可以编排的书时的两种版面（从 ws-author.jsx 拆出，2026-10）
   · ArrLoadingState —— 目录还在读，或者读不到（读不到不当成空作品，也不拿空目录覆盖服务端）；
   · ArrEmptyState —— 服务端确认这部作品还没有章：构思里的场已经列好就先给「整理章节结构」，否则先建第一章。
   children 是「整理章节结构」面板：确认写入时目录会整份重拉（WsCatalog.reset），面板要留在原地把这一步走完。
   ========================================================== */

export function ArrLoadingState({ error, onRetry, children }) {
  return (
    <div className="page arr-state" data-screen-label="author · loading">
      {error ? (
        <EmptyState icon="AlertTriangle" title="章节目录加载失败"
          actions={<button type="button" className="btn btn-ghost" onClick={onRetry}><I.Refresh size={13} /> 重试加载</button>}>
          系统不会把请求失败当成空作品，也不会用空目录覆盖服务端。
        </EmptyState>
      ) : (
        <div className="arr-loading" role="status"><Spinner size={16} /> 正在从服务端加载章节目录…</div>
      )}
      {children}
    </div>
  );
}

export function ArrEmptyState({ snowReady, onOpenPlan, onCreateFirst, onGoSnow, children }) {
  return (
    <div className="page arr-state" data-screen-label="author · empty">
      <EmptyState icon="Layers" title="这部作品还没有章节结构"
        actions={snowReady ? (
          <>
            <button type="button" className="btn btn-accent" data-testid="author-empty-open-plan" onClick={onOpenPlan}><I.Layout size={15} /> 整理章节结构</button>
            <button type="button" className="btn btn-ghost" onClick={onCreateFirst}><I.Plus size={15} /> 新建第一章</button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-accent" onClick={onCreateFirst}><I.Plus size={15} /> 新建第一章</button>
            <button type="button" className="btn btn-ghost" onClick={onGoSnow}>去构思</button>
          </>
        )}>
        {snowReady
          ? "构思里的场景已经列好了——把它们整理成章节，章和场就长到这里来；也可以自己从第一章建起。"
          : "章节编排从第一章开始；也可以先去雪花构思，把大纲长出来再回来编排。"}
      </EmptyState>
      {children}
    </div>
  );
}
