import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { wsNotify } from "./ws-notify.jsx";
import { useStoreTick } from "./lib/store-utils.js";
import { EmptyState } from "./ws-ui.jsx";
import { srErrorInfo, srSceneIndex } from "./ws-styleref-model.js";
import { srConfigureHost, srSubscribe } from "./ws-styleref-store.js";
import { isRealWorkId } from "./lib/work-id.js";

/* ==========================================================
   风格参考 · 各页共用的界面零件
   · srNotify / srActiveWork —— 提示、当前作品；当前作品在模块加载时经 srConfigureHost 交给 store
   · useSrStore —— 订阅 store 的频道重渲（lib/store-utils 的 useStoreTick）；useSrWorkScenes —— 当前作品的章与场（读目录 store）
   · SrErrorLine —— 出错的一句话 + 下一步按钮（去设置模型 / 打开这本 / 去学习文风）
   · SrStageEmpty（缺前一步时的空态卡）；页头「更多」与进度条用 ws-ui 的 MenuButton / ProgressBar（srProgressTone 给语气）
   不写 window。
   ========================================================== */

/* 失败 / 回执提示：外壳的提示层挂着时走应用内提示（ws-notify），没挂（单测里单独渲染）时退回 alert。 */
export function srNotify(message, tone = "danger") {
  wsNotify({ message, tone });
}

/* 出错时的一句话提示（按错误码给中文，见 srErrorInfo） */
export function srNotifyError(error, fallback) {
  srNotify(srErrorInfo(error, fallback).message);
}

/* 当前作品：书架还在加载（__loading__）或为空时返回 null */
export function srActiveWork() {
  try {
    const w = WsWorks && typeof WsWorks.active === "function" ? WsWorks.active() : null;
    if (w && isRealWorkId(w.id)) return { id: w.id, title: w.title || "" };
    const id = WsWorks && typeof WsWorks.activeId === "function" ? WsWorks.activeId() : null;
    return isRealWorkId(id) ? { id, title: "" } : null;
  } catch (e) {
    return null;
  }
}

srConfigureHost({
  activeWorkId: () => { const w = srActiveWork(); return w ? w.id : null; },
});

/* 订阅 store 的若干频道（books / detail / activity），有变化就重渲 */
export function useSrStore(...channels) {
  useStoreTick(srSubscribe(...channels));
}

/* 当前作品的章与场（本场预览、对照检查选一场、「像不像」的场名）：读目录 store WsCatalog（与章节编排、成本看板
   同一份，按当前作品缓存），不再自己整本 GET /catalog（审计 F05-14：三处各拉一遍、从不缓存）。
   目录还没读到时 chapters 为 null（界面说「正在读取」）；读不到时按没有场算。
   返回 { chapters, groups, labelOf, has }（groups / labelOf / has 见 srSceneIndex）。 */
export function useSrWorkScenes() {
  const catalog = useCatalogChapters();
  const loaded = WsCatalog.ready() || !!WsCatalog.loadError();
  const chapters = loaded ? catalog : null;
  const index = React.useMemo(() => srSceneIndex(chapters), [chapters]);
  return { chapters, ...index };
}

/* 出错的一句话 + 下一步。onAction(action) 由页面决定怎么走（去设置、打开书、跳到学习文风）。 */
export function SrErrorLine({ error, onAction, className, testId }) {
  if (!error) return null;
  const info = srErrorInfo(error);
  return (
    <p className={`sr-error-line${className ? ` ${className}` : ""}`} role="alert" data-testid={testId} title={info.code ? `错误代码：${info.code}` : undefined}>
      <I.AlertTriangle size={13} aria-hidden="true" />
      <span>{info.message}</span>
      {info.action && onAction && (
        <button type="button" className="btn btn-quiet btn-xs" data-testid={testId ? `${testId}-action` : undefined} onClick={() => onAction(info.action)}>
          {info.action.label}
        </button>
      )}
    </p>
  );
}

/* 进度条的语气（ws-ui ProgressBar 的 tone）：在跑 warn、做完 ok、没完成 danger、取消 neutral */
const SR_PROGRESS_TONE = { running: "warn", queued: "warn", succeeded: "ok", failed: "danger", cancelled: "neutral" };
export function srProgressTone(status) { return SR_PROGRESS_TONE[status] || undefined; }

/* 缺前一步时的空态卡：说现状，给下一步 */
export function SrStageEmpty({ icon = "Sparkles", title, children, actionLabel, onAction, testId }) {
  return (
    <div className="card sr-stage-empty-card" data-testid={testId}>
      <EmptyState icon={icon} title={title} actions={actionLabel ? <button type="button" className="btn btn-accent btn-sm" onClick={onAction}>{actionLabel}</button> : null}>
        {children}
      </EmptyState>
    </div>
  );
}
