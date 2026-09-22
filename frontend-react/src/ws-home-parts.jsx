import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WsAiProviders, useAiProviders } from "./ws-ai-providers.jsx";

/* ==========================================================
   主页各版面共用的小件：全书进度环、页顶的数据提示条（远端取数失败 / AI 未就绪 / 目录读不到）。
   ========================================================== */

/* 页顶提示条。三处（数据没更新、目录读不到、AI 未就绪）以前各抄一份同样的结构。
   role：失败用 alert，其余用 status（polite 播报）。action 是右侧那一个按钮。 */
function HmBanner({ icon = "AlertTriangle", role = "status", title, children, action }) {
  const Ic = I[icon] || I.AlertTriangle;
  return (
    <div className="hm-data-notice" role={role} aria-live={role === "status" ? "polite" : undefined}>
      <span className="hm-data-notice-ic" aria-hidden="true"><Ic size={15} /></span>
      <div>
        <strong>{title}</strong>
        <span>{children}</span>
      </div>
      {action}
    </div>
  );
}

/* 作品列表或 dashboard 读失败：本机缓存仍可用，但不能把失败藏进 console；重新连接走 store 的 retry。 */
function WsHomeDataNotice({ remote, workId }) {
  const dashboardError = remote && remote.dashboard && remote.dashboard.error;
  const projectsError = remote && remote.projects && remote.projects.error;
  const error = dashboardError || projectsError;
  if (!error) return null;
  const scope = dashboardError ? "dashboard" : "projects";
  const phase = scope === "dashboard" ? remote.dashboard.phase : remote.projects.phase;
  return (
    <HmBanner
      title={error.offline ? "当前离线，正在使用本机缓存" : "服务端数据暂时没有更新"}
      action={(
        <button type="button" className="btn btn-ghost btn-sm" disabled={phase === "loading"} onClick={() => { void WsWorks.retry(scope, workId); }}>
          <I.Refresh size={13} /> {phase === "loading" ? "重试中…" : "重新连接"}
        </button>
      )}
    >
      {error.message || "你的本机内容仍可继续使用。"}
    </HmBanner>
  );
}

/* 模型未就绪时在主页提前说明，避免作者走到 AI 动作后才遇到 409/502。
   compact：主页焦点卡按钮行里的一枚小按钮（状态晚到也不推动版面）；否则是页顶的整条提示。 */
function WsAiSetupNotice({ go, compact = false }) {
  const ai = useAiProviders();
  React.useEffect(() => {
    if (!ai.loaded && !ai.loading && !ai.error) {
      void WsAiProviders.refresh().catch(() => {});
    }
  }, [ai.loaded, ai.loading, ai.error]);
  const readiness = ai.overview && ai.overview.readiness;
  const globallyDisabled = ai.overview && ai.overview.api_snapshot && ai.overview.api_snapshot.enabled === false;
  if (!ai.loaded || !readiness || (readiness.ready === true && !globallyDisabled)) return null;
  const openSettings = () => go("settings", { type: "ws:settings-tab", detail: "ai" });
  if (compact) {
    return (
      <span className="hm-ai-flag" role="status" aria-live="polite">
        <button type="button" className="btn btn-quiet btn-sm" onClick={openSettings}
          title="开始生成前，请先在设置里配置并启用一个可用模型。">
          <I.Sparkles size={13} /> AI 尚未就绪，去配置
        </button>
      </span>
    );
  }
  return (
    <HmBanner icon="Sparkles" title="AI 尚未就绪"
      action={<button type="button" className="btn btn-ghost btn-sm" onClick={openSettings}><I.Settings size={13} /> 去配置</button>}>
      开始生成前，请先在设置里配置并启用一个可用模型。
    </HmBanner>
  );
}

/* 全书进度环（按字数口径）。渐变 id 每个实例一份，同页两枚环不会互相串色。 */
function HomeRing({ pct, size = 132 }) {
  const sw = Math.max(6, Math.round(size * 0.105));
  const r = (size - sw) / 2 - 1, c = 2 * Math.PI * r, dash = (pct / 100) * c, cx = size / 2;
  const gradientId = `hm-ring-${React.useId().replace(/[^A-Za-z0-9_-]/g, "")}`;
  return (
    <svg className="hm-ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`完成 ${pct}%`}>
      <defs>
        <linearGradient id={gradientId} x1="0" x2="1" y1="0" y2="1">
          <stop offset="0%" stopColor="var(--crimson)" />
          <stop offset="100%" stopColor="var(--gold)" />
        </linearGradient>
      </defs>
      <circle cx={cx} cy={cx} r={r} fill="none" stroke="var(--line-1)" strokeWidth={sw} />
      <circle cx={cx} cy={cx} r={r} fill="none" stroke={`url(#${gradientId})`} strokeWidth={sw} strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={c - dash} transform={`rotate(-90 ${cx} ${cx})`} />
      <text x={cx} y={cx + size * 0.055} textAnchor="middle"
        style={{ fontSize: size * 0.3, fontWeight: 600, fill: "var(--ink-1)", fontFamily: "var(--font-serif)" }}>
        {pct}<tspan fontSize={size * 0.155} dy={-size * 0.04}>%</tspan>
      </text>
    </svg>
  );
}

export { HmBanner, HomeRing, WsAiSetupNotice, WsHomeDataNotice };
