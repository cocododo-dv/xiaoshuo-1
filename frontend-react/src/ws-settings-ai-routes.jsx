import React from "react";
import { I } from "./icons.jsx";
import { WsAiProviders } from "./ws-ai-providers.jsx";
import { errText, providerHealth, providerUsable, reasonKey } from "./ws-settings-ai-health.js";
import { Section, Row, Field } from "./ws-settings-shared.jsx";
import { Notice, Tag } from "./ws-ui.jsx";
import { wsConfirm } from "./ws-notify.jsx";

/* ==========================================================
   设置 · AI 模型 → 接入状态（没就绪的原因按原因 + 服务分组，一步修好）、
   模型分工（三个角色槽位批量路由）、高级路由（按分工只读列出每个 AI 功能）。
   ========================================================== */

const { useState, useMemo } = React;

/* 高级路由里每个 AI 功能的中文名。后端节点目录只有英文 label（节点 id 放在悬停提示里备查）；
   不在表里的新节点退回后端 label。 */
const NODE_LABEL_ZH = {
  author_proposal_generate: "作者提案生成",
  chapter_near_final_review: "章节终审",
  chapter_plan_review: "章节规划审阅",
  chapter_scene_plan_candidates: "章内场景方案候选",
  chapter_scene_plan_fill: "章内场景方案补全",
  chapter_story_architecture: "章节故事架构",
  character_pressure_blueprint: "人物压力蓝图",
  extraction: "资料抽取",
  hard_qc: "硬性质检",
  near_final_acceptance_review: "场景终稿验收",
  neutral_draft: "场景初稿",
  scene_blueprint: "场景蓝图",
  scene_literary_rewrite: "场景文学改写",
  snowflake_chapter_plan: "构思：分章与起章名",
  snowflake_scene_triage: "构思：场景体检",
  snowflake_step_candidates: "构思：先看 3 个方向",
  snowflake_step_generate: "构思：生成本步",
  snowflake_workspace_assistant: "构思：教练",
  soft_qc: "文学质检",
  style_draft: "风格稿",
  style_patch: "风格修补",
  style_ref_extract_language: "参考书：抽取语言层",
  style_ref_extract_narrative: "参考书：抽取叙事层",
  style_ref_extract_scene: "参考书：抽取场景层",
  style_ref_extract_theme: "参考书：抽取主题层",
  style_ref_paragraph_classify_anchor: "参考书：段落分类（锚定集）",
  style_ref_paragraph_classify_bulk: "参考书：段落分类（其余段落）",
  style_ref_preview_generate: "参考书：示例预览",
  style_ref_supplement_evidence: "参考书：补抽证据",
  style_ref_synthesize_profile: "参考书：合成画像",
  writer_deep_review: "写作台：深度审读",
  writer_passage_patch: "写作台：段落修改",
};
const nodeLabel = (route) => NODE_LABEL_ZH[route.node_id] || route.label || route.node_id;

/* 路由没就绪的原因 → 一句中文短语（高级路由的行尾用） */
const REASON_SHORT = {
  secret_decrypt_failed: "密钥解不开",
  secret_missing: "未填密钥",
  provider_disabled: "服务已停用",
  provider_not_ready: "服务不可用",
  provider_not_found: "服务已删除",
  provider_id_missing: "未选服务",
  model_missing: "未选模型",
  model_not_listed: "模型不在列表里",
  not_configured: "未指派",
};

/* ===== 没就绪的原因：按原因 + 服务分组，每组一条提示和一个能直接动手的按钮 ===== */
function blockedGroups(overview) {
  const blocked = (overview.readiness && overview.readiness.blocked_routes) || overview.blocked_routes || [];
  const map = new Map();
  blocked.forEach((route) => {
    const key = reasonKey(route.reason);
    const providerId = route.provider_id || "";
    const id = `${key}|${providerId}`;
    if (!map.has(id)) map.set(id, { key, providerId, count: 0, models: new Set() });
    const group = map.get(id);
    group.count += 1;
    if (route.model) group.models.add(route.model);
  });
  return Array.from(map.values());
}

function blockedCopy(group) {
  const pid = group.providerId || "未命名服务";
  const tail = `受影响的 AI 功能：${group.count} 个。`;
  switch (group.key) {
    case "secret_decrypt_failed":
      return {
        tone: "danger",
        title: `「${pid}」的 API 密钥解不开了`,
        text: `本机保存密钥用的那把钥匙换过了（比如重装、换了启动方式），之前存下的 API 密钥读不出来。重新填一次这个服务的 API 密钥就能恢复。${tail}`,
        action: { kind: "edit", field: "api_key", label: "重新填写密钥" },
      };
    case "secret_missing":
      return {
        tone: "warn",
        title: `「${pid}」还没有填 API 密钥`,
        text: `填好密钥后，用它的 AI 功能才能工作。${tail}`,
        action: { kind: "edit", field: "api_key", label: "填写密钥" },
      };
    case "provider_disabled":
      return {
        tone: "warn",
        title: `「${pid}」已停用`,
        text: `在它的服务卡片上打开「启用」，或在「模型分工」里换一个服务。${tail}`,
        action: { kind: "card", label: "查看服务" },
      };
    case "model_not_listed":
      return {
        tone: "warn",
        title: `分工用的模型不在「${pid}」的模型列表里`,
        text: `缺的模型：${Array.from(group.models).join("、") || "未知"}。在服务里补上这个模型，或在「模型分工」里换一个。${tail}`,
        action: { kind: "edit", field: "models", label: "编辑服务" },
      };
    case "provider_not_found":
      return {
        tone: "warn",
        title: `分工指向的服务「${pid}」已经不在了`,
        text: `在下方「模型分工」里重新选一个服务。${tail}`,
        action: { kind: "slots", label: "去模型分工" },
      };
    default:
      return {
        tone: "warn",
        title: group.providerId ? `「${pid}」暂时不可用` : "有些 AI 功能还没选服务或模型",
        text: `检查下方的服务卡片和「模型分工」。${tail}`,
        action: group.providerId ? { kind: "card", label: "查看服务" } : { kind: "slots", label: "去模型分工" },
      };
  }
}

/* ===== 接入状态条 + 管理令牌 ===== */
function StatusSection({ state, onFix }) {
  const overview = state.overview || {};
  const readiness = overview.readiness || {};
  const ready = readiness.ready === true;
  const [token, setToken] = useState(WsAiProviders.adminToken());
  const groups = blockedGroups(overview);
  return (
    <Section title="接入状态" desc="模型服务和各项 AI 功能是否就绪。就绪后，所有 AI 功能都按下方「模型分工」调用。">
      <Row label="当前状态" readonly>
        <Tag tone={ready ? "ok" : "danger"} dot>{ready ? "就绪" : "未就绪"}</Tag>
        <span className="set-readonly">
          可用服务 {readiness.active_provider_count ?? 0}/{readiness.provider_count ?? 0}，就绪功能 {readiness.ready_route_count ?? 0}/{readiness.active_route_count ?? 0}
        </span>
      </Row>
      {groups.length > 0 && (
        <div className="set-notices">
          {groups.map((group) => {
            const copy = blockedCopy(group);
            return (
              <Notice key={`${group.key}|${group.providerId}`} tone={copy.tone} title={copy.title}
                actions={copy.action ? (
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => onFix(copy.action, group.providerId)}>{copy.action.label}</button>
                ) : null}>
                {copy.text}
              </Notice>
            );
          })}
        </div>
      )}
      {state.adminConfigured && (
        <Row label="管理令牌" hint="后端要求管理令牌（X-Admin-Token）；保存服务、改分工都要用到。只存在这个浏览器会话里。">
          <Field className="input set-field" type="password" placeholder="管理令牌" value={token}
            onChange={(e) => setToken(e.target.value)}
            onBlur={() => WsAiProviders.setAdminToken(token)} />
        </Row>
      )}
    </Section>
  );
}

/* ===== 模型分工（角色槽位） ===== */
function RoleSlotsSection({ state, setFlash }) {
  const overview = state.overview || {};
  const slots = overview.role_slots || [];
  const providers = overview.providers || {};
  const providerIds = Object.keys(providers);
  const [draft, setDraft] = useState({});
  const busy = state.busy["role-routes"];

  const slotValue = (slot) => draft[slot.slot_id] || {
    provider_id: slot.current?.provider_id || "",
    model: slot.current?.model || "",
  };
  /* 用不了的服务（停用 / 没密钥 / 密钥解不开）照样列出来，但标明原因且不可选——当前正用着的除外 */
  const providerOption = (id, currentId) => {
    const p = providers[id];
    const health = providerHealth(p).filter(h => h.tone !== "warn" || h.text !== "没有模型");
    const usable = providerUsable(p);
    const suffix = usable ? "" : `（${(health[0] && health[0].text) || "不可用"}）`;
    return <option key={id} value={id} disabled={!usable && id !== currentId}>{id}{suffix}</option>;
  };

  const apply = async () => {
    const assignments = {};
    slots.forEach(slot => {
      const v = slotValue(slot);
      if (v.provider_id && v.model) assignments[slot.slot_id] = { provider_id: v.provider_id, model: v.model };
    });
    if (!Object.keys(assignments).length) {
      setFlash({ tone: "err", text: "先给至少一个分工选好服务和模型。" });
      return;
    }
    const ok = await wsConfirm({
      title: "应用这套分工？",
      body: "会改写这些分工里每个 AI 功能用的模型，包括你在高级路由里单独调过的那些。",
      confirmLabel: "应用分工",
    });
    if (!ok) return;
    WsAiProviders.saveRoleRoutes(assignments, true)
      .then(() => { setDraft({}); setFlash({ tone: "ok", text: "分工已应用，对应 AI 功能的模型已经生效。" }); })
      .catch((error) => setFlash({ tone: "err", text: errText(error, "应用分工失败。") }));
  };

  return (
    <Section title="模型分工" desc="不用逐个功能配置：按写作场景把模型分成三班岗，应用后立即生效。" id="set-role-slots">
      {slots.map(slot => {
        const v = slotValue(slot);
        const models = v.provider_id ? (providers[v.provider_id]?.models || []) : [];
        const modelMissing = v.model && !models.includes(v.model);
        const mixed = slot.current?.mixed;
        return (
          <Row key={slot.slot_id} label={slot.label_zh}
            hint={`${slot.description_zh}（${slot.node_ids.length} 个 AI 功能${mixed ? "，目前各用各的模型" : ""}）`}>
            <div className="set-slot-ctl">
              <Field as="select" className="select set-select" value={v.provider_id} title={v.provider_id || "选择服务"}
                onChange={(e) => setDraft(d => ({ ...d, [slot.slot_id]: { provider_id: e.target.value, model: "" } }))}>
                <option value="">选择服务…</option>
                {providerIds.map(id => providerOption(id, slot.current?.provider_id))}
              </Field>
              <select className="select set-select set-model-select" value={v.model} disabled={!v.provider_id}
                aria-label={`${slot.label_zh}的模型`} title={v.model || "选择模型"}
                onChange={(e) => setDraft(d => ({ ...d, [slot.slot_id]: { provider_id: v.provider_id, model: e.target.value } }))}>
                <option value="">选择模型…</option>
                {modelMissing && <option value={v.model}>{v.model}（不在列表里）</option>}
                {models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>
          </Row>
        );
      })}
      <div className="set-form-actions">
        <button type="button" className="btn btn-accent" disabled={busy} onClick={apply}>{busy ? "应用中…" : "应用分工"}</button>
      </div>
    </Section>
  );
}

/* ===== 高级路由：按分工列出每个 AI 功能（折叠；只看不改） ===== */
function AdvancedRoutes({ state, setFlash }) {
  const overview = state.overview || {};
  const nodeRoutes = overview.node_routes || {};
  const providers = overview.providers || {};
  const slots = overview.role_slots || [];
  const missing = overview.missing_active_routes || [];
  const active = useMemo(() => Object.values(nodeRoutes)
    .filter(r => r.status === "active" && r.requires_llm !== false)
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0)), [nodeRoutes]);
  const groups = useMemo(() => {
    const claimed = new Set();
    const out = slots.map(slot => {
      const ids = new Set(slot.node_ids || []);
      const routes = active.filter(r => ids.has(r.node_id));
      routes.forEach(r => claimed.add(r.node_id));
      return { id: slot.slot_id, label: slot.label_zh, desc: slot.description_zh, routes };
    }).filter(g => g.routes.length);
    const rest = active.filter(r => !claimed.has(r.node_id));
    if (rest.length) out.push({ id: "other", label: "其他", desc: "不在三个分工里的 AI 功能", routes: rest });
    return out;
  }, [active, slots]);
  const readyN = active.filter(r => r.ready).length;

  return (
    <details className="set-section set-advanced">
      <summary className="set-advanced-summary">
        <span className="set-advanced-title">
          <span className="set-section-title text-serif">高级路由</span>
          <span className="set-advanced-count">{readyN}/{active.length} 个 AI 功能就绪{missing.length ? `，${missing.length} 个没有指派` : ""}</span>
        </span>
        <span className="set-section-desc">按分工查看每个 AI 功能用的服务与模型。</span>
        <I.ChevronDown className="set-advanced-chev" size={16} aria-hidden="true" />
      </summary>
      <div className="set-advanced-body">
        {missing.length > 0 && (
          <Notice tone="warn" title={`有 ${missing.length} 个 AI 功能还没有指派模型`}
            actions={(
              <button type="button" className="btn btn-ghost btn-sm" disabled={state.busy["sync-missing"]}
                onClick={() => WsAiProviders.syncMissing({})
                  .then((r) => setFlash({ tone: "ok", text: `已用默认服务补齐 ${r.synced_node_ids?.length ?? 0} 个 AI 功能。` }))
                  .catch((error) => setFlash({ tone: "err", text: errText(error, "补齐路由失败。") }))}>
                用默认服务补齐
              </button>
            )}>
            会给它们指派默认服务的默认模型。
          </Notice>
        )}
        {groups.map(group => (
          <div key={group.id} className="set-route-group">
            <div className="set-route-group-head">
              <span className="set-route-group-label">{group.label}</span>
              <span className="set-route-group-desc">{group.desc}</span>
            </div>
            {group.routes.map(route => {
              const key = reasonKey(route.readiness_reason);
              const reason = route.ready ? "" : (REASON_SHORT[key] || "未就绪");
              return (
                <div key={route.node_id} className={`set-route ${route.ready ? "is-ready" : "is-blocked"}`}>
                  <span className="set-route-dot" aria-hidden="true" />
                  <span className="set-route-label" title={route.node_id}>{nodeLabel(route)}</span>
                  <span className="set-route-target" title={route.provider_id ? `${route.provider_id} / ${route.model}` : ""}>
                    {route.provider_id ? `${route.provider_id} / ${route.model}` : "未指派"}
                  </span>
                  {reason && <span className="set-route-reason">{reason}</span>}
                </div>
              );
            })}
          </div>
        ))}
        <p className="set-row-hint set-advanced-foot">
          这里只看不改：换模型请在上面的「模型分工」里整班改；温度、输出上限等单项参数由后端的模型配置文件决定。
          {Object.keys(providers).length === 0 ? "先接入一个模型服务。" : ""}
        </p>
      </div>
    </details>
  );
}

export { StatusSection, RoleSlotsSection, AdvancedRoutes };
