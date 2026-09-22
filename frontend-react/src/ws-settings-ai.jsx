import React from "react";
import { WsAiProviders, useAiProviders } from "./ws-ai-providers.jsx";
import { errText } from "./ws-settings-ai-health.js";
import { ProvidersSection } from "./ws-settings-ai-providers.jsx";
import { AdvancedRoutes, RoleSlotsSection, StatusSection } from "./ws-settings-ai-routes.jsx";
import { Section } from "./ws-settings-shared.jsx";
import { CloseButton, Notice } from "./ws-ui.jsx";

/* ==========================================================
   AISettings — 设置 → AI 模型（真实模型接入）
   ----------------------------------------------------------
   · 接入状态：是否就绪 + 没就绪的原因（按原因分组，说人话，并给出一步就能修的按钮）+ 管理令牌
   · 模型服务：已接入服务卡片（启用 / 测试 / 默认 / 编辑 / 删除）+ 预设添加流程
   · 模型分工：写作主力 / 审稿质检 / 提炼整理 三个分工批量路由
   · 高级路由：按分工列出每个 AI 功能用的服务与模型（只看不改），一键补齐缺失路由
   2026-09-21：删掉「AI 行为 · 生成候选数」（只写 localStorage，没有代码读取）；内联样式收进 screens.css 的 .set-* 类。
   这个文件只管页面骨架与提示条：服务卡片与表单在 ws-settings-ai-providers.jsx，
   接入状态 / 分工 / 高级路由在 ws-settings-ai-routes.jsx，共用的纯函数在 ws-settings-ai-health.js。
   ========================================================== */

const { useState, useEffect } = React;

function Flash({ flash, onClose }) {
  if (!flash) return null;
  return (
    <Notice
      tone={flash.tone === "ok" ? "ok" : "danger"}
      className="set-flash"
      actions={(
        <>
          {flash.action && <button type="button" className="btn btn-ghost btn-sm" onClick={flash.action.run}>{flash.action.label}</button>}
          <CloseButton onClick={onClose} />
        </>
      )}
    >
      {flash.text}
    </Notice>
  );
}

/* ===== 主入口 ===== */
function AISettings() {
  const state = useAiProviders();
  const [flash, setFlash] = useState(null);
  const [editing, setEditing] = useState({}); // { [provider_id]: { field } } —— 每张卡独立原位展开编辑

  useEffect(() => {
    if (!state.loaded && !state.loading) {
      WsAiProviders.refresh().catch(() => {});
      WsAiProviders.loadPresets().catch(() => {});
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const openEdit = (id, field = null) => {
    WsAiProviders.loadPresets().catch(() => {});
    setEditing(e => ({ ...e, [id]: { field } }));
  };
  const closeEdit = (id) => setEditing(({ [id]: _closed, ...rest }) => rest);
  const scrollTo = (elementId) => {
    const el = typeof document !== "undefined" ? document.getElementById(elementId) : null;
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  /* 接入状态里「一步修好」的按钮 */
  const fix = (action, providerId) => {
    if (action.kind === "edit" && providerId && state.overview?.providers?.[providerId]) { openEdit(providerId, action.field); return; }
    if (action.kind === "card" && providerId) { scrollTo(`set-provider-${providerId}`); return; }
    scrollTo("set-role-slots");
  };

  if (state.error && !state.overview) {
    return (
      <Section title="模型接入" desc="读取后端的模型配置失败。">
        <Notice tone="danger" actions={<button type="button" className="btn btn-ghost btn-sm" onClick={() => WsAiProviders.refresh().catch(() => {})}>重试</button>}>
          {errText(state.error, "读取失败。")}
        </Notice>
      </Section>
    );
  }
  if (!state.overview) {
    return <Section title="模型接入" desc="正在读取后端的模型配置…"><div className="set-readonly">加载中…</div></Section>;
  }

  return (
    <>
      <Flash flash={flash} onClose={() => setFlash(null)} />
      <StatusSection state={state} onFix={fix} />
      <ProvidersSection state={state} setFlash={setFlash} editing={editing} onEdit={(id) => openEdit(id)} onCloseEdit={closeEdit} />
      <RoleSlotsSection state={state} setFlash={setFlash} />
      <AdvancedRoutes state={state} setFlash={setFlash} />
    </>
  );
}

export { AISettings };
