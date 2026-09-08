import React from "react";
import { I } from "./icons.jsx";
import { WsAiProviders, useAiProviders } from "./ws-ai-providers.jsx";
import { Section, Row, Toggle, Segmented, usePref } from "./ws-settings-shared.jsx";

/* ==========================================================
   AISettings — 设置 → AI 模型(真实模型接入)
   ----------------------------------------------------------
   · 接入状态:readiness 摘要 + 管理令牌(后端配置了令牌时)
   · 模型服务:已接入服务卡片(测试/默认/编辑)+ 预设添加流程
   · 模型分工:写作主力 / 审稿质检 / 提炼整理 三槽位批量路由
   · 高级路由:全节点矩阵(折叠),一键补齐缺失路由
   · AI 行为:本地偏好(候选数 / 参考画像 / 复刻严格度)
   ========================================================== */

const { useState, useEffect, useMemo } = React;

const CATEGORY_LABELS = {
  cn: "国内厂商",
  international: "国际厂商",
  relay: "第三方中转",
  local: "本地",
  custom: "自定义",
};
const CATEGORY_ORDER = ["cn", "international", "relay", "local", "custom"];

function splitModels(text) {
  return Array.from(new Set(String(text || "")
    .split(/[\n,，、;；\s]+/)
    .map(s => s.trim())
    .filter(Boolean)));
}

function errText(error, fallback) {
  if (!error) return fallback;
  if (error.code === "ADMIN_TOKEN_REQUIRED") return "管理令牌缺失或不正确——在上方「接入状态」里填入后端的管理令牌后重试。";
  return error.message || fallback;
}

function Flash({ flash, onClose }) {
  if (!flash) return null;
  const tone = flash.tone === "ok" ? "var(--sage, #5a7d5a)" : "var(--rose, #b04a4a)";
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: 8, padding: "10px 12px",
      border: `1px solid color-mix(in srgb, ${tone} 35%, transparent)`,
      background: `color-mix(in srgb, ${tone} 7%, transparent)`,
      borderRadius: 8, fontSize: 13, color: "var(--ink-1)", margin: "8px 0",
    }}>
      <span style={{ flex: 1, whiteSpace: "pre-wrap" }}>{flash.text}</span>
      {flash.action && (
        <button className="btn btn-ghost" style={{ flexShrink: 0 }} onClick={flash.action.run}>{flash.action.label}</button>
      )}
      <button className="btn btn-quiet" style={{ flexShrink: 0 }} onClick={onClose} aria-label="关闭"><I.X size={13} /></button>
    </div>
  );
}

/* ===== 接入状态条 + 管理令牌 ===== */
function StatusSection({ state }) {
  const readiness = state.overview?.readiness || {};
  const ready = readiness.ready === true;
  const [token, setToken] = useState(WsAiProviders.adminToken());
  return (
    <Section title="接入状态" desc="模型服务与节点路由是否就绪;就绪后所有 AI 功能都会经由下方分工调用。">
      <Row label="当前状态">
        <span className={`pill ${ready ? "pill-sage" : "pill-rose"}`}>
          <span className="pill-dot" />{ready ? "就绪" : "未就绪"}
        </span>
        <span className="set-readonly">
          服务 {readiness.active_provider_count ?? 0}/{readiness.provider_count ?? 0} 可用 ·
          路由 {readiness.ready_route_count ?? 0}/{readiness.active_route_count ?? 0} 就绪
          {readiness.blocked_route_count ? ` · ${readiness.blocked_route_count} 个被阻塞` : ""}
        </span>
      </Row>
      {state.adminConfigured && (
        <Row label="管理令牌" hint="后端启用了 X-Admin-Token;保存服务、改路由需要它。即填即存,仅存于本机。">
          <input className="input" type="password" placeholder="管理令牌" value={token}
            onChange={(e) => setToken(e.target.value)}
            onBlur={() => WsAiProviders.setAdminToken(token)} />
        </Row>
      )}
    </Section>
  );
}

/* ===== 服务卡片 ===== */
function ProviderCard({ id, provider, state, onEdit, setFlash }) {
  const overview = state.overview || {};
  const isDefault = overview.default_provider_id === id;
  const catalog = state.presets?.provider_catalog || overview.provider_catalog || {};
  const typeLabel = catalog[provider.provider_type]?.label || provider.provider_type;
  const probe = state.probes[id];
  const busyProbe = state.busy[`probe:${id}`];
  const busyDelete = state.busy[`delete:${id}`];
  const keyHint = provider.credential_mode === "none"
    ? "免密钥"
    : (provider.secret?.configured ? `密钥 ${provider.secret.hint || "已配置"}` : "密钥未配置");

  const toggleEnabled = (on) => {
    WsAiProviders.saveProvider({ ...provider, provider_id: id, enabled: on })
      .catch((error) => setFlash({ tone: "err", text: errText(error, "更新服务状态失败。") }));
  };

  const remove = () => {
    if (!window.confirm(`确认删除服务「${id}」?它的密钥会一并从后端删除;仍指向它的节点路由会变为未就绪。`)) return;
    WsAiProviders.deleteProvider(id)
      .then((result) => {
        const orphaned = result?.orphaned_route_node_ids || [];
        setFlash(orphaned.length ? {
          tone: "ok",
          text: `服务「${id}」已删除。有 ${orphaned.length} 个 AI 节点的路由仍指向它,已标记为未就绪。`,
          action: {
            label: "一键补齐路由",
            run: () => WsAiProviders.syncMissing({})
              .then((r) => setFlash({ tone: "ok", text: `已用默认服务补齐 ${r.synced_node_ids?.length ?? 0} 个节点的路由。` }))
              .catch((error) => setFlash({ tone: "err", text: errText(error, "补齐路由失败。") })),
          },
        } : { tone: "ok", text: `服务「${id}」已删除。` });
      })
      .catch((error) => setFlash({ tone: "err", text: errText(error, "删除服务失败。") }));
  };

  return (
    <div className="card-flat" style={{ padding: "12px 14px", marginBottom: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <strong style={{ fontSize: 14 }}>{id}</strong>
        <span className="pill pill-slate">{typeLabel}</span>
        {isDefault && <span className="pill pill-gold">默认</span>}
        <span className="set-readonly" style={{ marginLeft: "auto" }}>{keyHint}</span>
        <Toggle on={provider.enabled !== false} onChange={toggleEnabled} />
      </div>
      <div className="set-readonly" style={{ marginTop: 6, wordBreak: "break-all" }}>{provider.base_url}</div>
      {Array.isArray(provider.models) && provider.models.length > 0 && (
        <div className="set-readonly" style={{ marginTop: 4 }}>
          模型:{provider.models.slice(0, 4).join(" · ")}{provider.models.length > 4 ? ` 等 ${provider.models.length} 个` : ""}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
        <button className="btn btn-ghost" disabled={busyProbe}
          onClick={() => WsAiProviders.probe(id).catch(() => {})}>
          {busyProbe ? "测试中…" : "测试连接"}
        </button>
        {!isDefault && (
          <button className="btn btn-ghost" disabled={state.busy[`default:${id}`]}
            onClick={() => WsAiProviders.setDefault(id).catch((error) => setFlash({ tone: "err", text: errText(error, "设为默认失败。") }))}>
            设为默认
          </button>
        )}
        <button className="btn btn-ghost" onClick={() => onEdit(id)}>编辑</button>
        <button className="btn btn-ghost" disabled={busyDelete} style={{ color: "var(--rose, #b04a4a)" }} onClick={remove}>
          {busyDelete ? "删除中…" : "删除"}
        </button>
      </div>
      {probe && (
        <div className="set-readonly" style={{
          marginTop: 8, padding: "8px 10px", borderRadius: 6,
          background: "var(--paper-2)", fontSize: 12.5,
          color: probe.ok ? "var(--ink-2)" : "var(--rose)",
        }}>
          {probe.ok ? "✓" : "✕"} {probe.message}
          {typeof probe.latency_ms === "number" ? ` · ${probe.latency_ms}ms` : ""}
        </div>
      )}
    </div>
  );
}

/* ===== 添加 / 编辑服务表单 ===== */
function ProviderForm({ state, preset, editingId, onDone, setFlash }) {
  const overview = state.overview || {};
  const editing = editingId ? overview.providers?.[editingId] : null;
  const [form, setForm] = useState(() => editing ? {
    provider_id: editingId,
    provider_type: editing.provider_type,
    base_url: editing.base_url || "",
    api_key: "",
    credential_mode: editing.credential_mode || "api_key",
    api_mode: editing.api_mode || "chat",
    modelsText: (editing.models || []).join("\n"),
  } : {
    provider_id: preset.preset_id === "custom" ? "" : preset.preset_id,
    provider_type: preset.provider_type,
    base_url: preset.default_base_url || "",
    api_key: "",
    credential_mode: preset.credential_modes.includes("api_key") ? "api_key" : "none",
    api_mode: preset.default_api_mode || "chat",
    modelsText: (preset.common_models || []).join("\n"),
  });
  const [testResult, setTestResult] = useState(null);
  const [saving, setSaving] = useState(false);
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const credentialChoices = editing
    ? (state.presets?.provider_catalog?.[editing.provider_type]?.credential_modes || ["api_key"])
    : preset.credential_modes;
  const models = splitModels(form.modelsText);

  const draftPayload = () => ({
    provider_id: form.provider_id.trim(),
    provider_type: form.provider_type,
    base_url: form.base_url.trim(),
    credential_mode: form.credential_mode,
    api_mode: form.api_mode,
    ...(form.api_key.trim() ? { api_key: form.api_key.trim() } : {}),
  });

  const pullModels = async () => {
    setTestResult(null);
    try {
      const result = editingId && !form.api_key.trim()
        ? await WsAiProviders.fetchModels(editingId)
        : await WsAiProviders.testDraft({ ...draftPayload(), check_completion: false });
      const available = result.available_models || [];
      if (available.length) {
        set("modelsText", available.join("\n"));
        setTestResult({ ok: true, message: `拉取到 ${available.length} 个模型${result.source === "preset" ? "(来自预设)" : ""}` });
      } else {
        setTestResult({ ok: false, message: result.message || "服务没有返回模型列表,请手动填写。" });
      }
    } catch (error) {
      setTestResult({ ok: false, message: errText(error, "拉取模型失败。") });
    }
  };

  const testConnection = async () => {
    setTestResult(null);
    try {
      const result = await WsAiProviders.testDraft({
        ...draftPayload(),
        model: models[0] || undefined,
        check_completion: Boolean(models[0]),
      });
      setTestResult({ ok: result.ok, message: `${result.message}${typeof result.latency_ms === "number" ? ` · ${result.latency_ms}ms` : ""}` });
    } catch (error) {
      setTestResult({ ok: false, message: errText(error, "连接测试失败。") });
    }
  };

  const save = async () => {
    if (!form.provider_id.trim()) { setTestResult({ ok: false, message: "先给这个服务起一个标识(如 my-openai)。" }); return; }
    if (!form.base_url.trim()) { setTestResult({ ok: false, message: "base_url 不能为空。" }); return; }
    setSaving(true);
    try {
      await WsAiProviders.saveProvider({ ...draftPayload(), enabled: true, models });
      const missing = WsAiProviders.state().overview?.missing_active_routes || [];
      setFlash(missing.length ? {
        tone: "ok",
        text: `服务「${form.provider_id.trim()}」已保存。还有 ${missing.length} 个 AI 节点未指派模型。`,
        action: {
          label: "一键补齐路由",
          run: () => WsAiProviders.syncMissing({ provider_id: form.provider_id.trim() })
            .then((r) => setFlash({ tone: "ok", text: `已补齐 ${r.synced_node_ids?.length ?? 0} 个节点的路由。` }))
            .catch((error) => setFlash({ tone: "err", text: errText(error, "补齐路由失败。") })),
        },
      } : { tone: "ok", text: `服务「${form.provider_id.trim()}」已保存。` });
      onDone();
    } catch (error) {
      setTestResult({ ok: false, message: errText(error, "保存失败。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card-flat" style={{ padding: "14px 16px", margin: editing ? "0 0 8px" : "8px 0 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <strong>{editing ? `编辑「${editingId}」` : `添加:${preset.label_zh}`}</strong>
        {!editing && preset.notes_zh && <span className="set-row-hint" style={{ marginTop: 0 }}>{preset.notes_zh}</span>}
      </div>
      {!editing && (
        <Row label="服务标识" hint="本系统内的名字,保存后不可改;一个厂商可以接多个账号。">
          <input className="input" value={form.provider_id} placeholder="如 my-deepseek"
            onChange={(e) => set("provider_id", e.target.value)} />
        </Row>
      )}
      <Row label="接口地址" hint="API base URL;第三方中转填它给你的地址。">
        <input className="input" style={{ minWidth: 300 }} value={form.base_url}
          onChange={(e) => set("base_url", e.target.value)} />
      </Row>
      {credentialChoices.length > 1 && (
        <Row label="鉴权方式">
          <Segmented options={[
            { value: "api_key", label: "API 密钥" },
            { value: "none", label: "免密钥" },
          ]} value={form.credential_mode} onChange={(v) => set("credential_mode", v)} />
        </Row>
      )}
      {form.credential_mode === "api_key" && (
        <Row label="API 密钥" hint={editing?.secret?.configured ? `已配置(${editing.secret.hint});留空则沿用。` : "只会加密存储在后端,不入前端缓存。"}>
          <input className="input" type="password" value={form.api_key} placeholder={editing?.secret?.configured ? "留空沿用现有密钥" : "sk-…"}
            onChange={(e) => set("api_key", e.target.value)} />
        </Row>
      )}
      <Row label="模型列表" hint="一行一个;路由与分工只能从这里选。「拉取模型」自动获取。">
        <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-end" }}>
          <textarea className="textarea" rows={4} style={{ width: 280 }} value={form.modelsText}
            onChange={(e) => set("modelsText", e.target.value)} />
          <button className="btn btn-ghost" disabled={state.busy["draft-test"]} onClick={pullModels}>
            {state.busy["draft-test"] ? "拉取中…" : "拉取模型"}
          </button>
        </div>
      </Row>
      {testResult && (
        <div className="set-readonly" style={{ padding: "8px 10px", borderRadius: 6, background: "var(--paper-2)", color: testResult.ok ? "var(--ink-2)" : "var(--rose)", fontSize: 12.5 }}>
          {testResult.ok ? "✓" : "✕"} {testResult.message}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button className="btn btn-ghost" disabled={state.busy["draft-test"]} onClick={testConnection}>连接测试</button>
        <button className="btn btn-primary" disabled={saving} onClick={save}>{saving ? "保存中…" : "保存服务"}</button>
        <button className="btn btn-quiet" onClick={onDone}>取消</button>
      </div>
    </div>
  );
}

/* ===== 预设选择(添加流程第一步) ===== */
function PresetPicker({ state, onPick, onClose }) {
  const presets = state.presets?.presets || [];
  const grouped = useMemo(() => {
    const map = new Map();
    presets.forEach(p => {
      if (!map.has(p.category)) map.set(p.category, []);
      map.get(p.category).push(p);
    });
    return CATEGORY_ORDER.filter(c => map.has(c)).map(c => [c, map.get(c)]);
  }, [presets]);

  if (!presets.length) {
    return <div className="set-readonly" style={{ padding: 8 }}>预设目录加载中…(若后端未启动会一直停在这里)</div>;
  }
  return (
    <div className="card-flat" style={{ padding: "14px 16px", marginTop: 8 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
        <strong>选择厂商或接入方式</strong>
        <button className="btn btn-quiet" style={{ marginLeft: "auto" }} onClick={onClose}>取消</button>
      </div>
      {grouped.map(([category, items]) => (
        <div key={category} style={{ marginBottom: 10 }}>
          <div className="set-row-hint" style={{ marginBottom: 6 }}>{CATEGORY_LABELS[category] || category}</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {items.map(p => (
              <button key={p.preset_id} className="btn btn-ghost" onClick={() => onPick(p)}
                title={p.notes_zh || ""}>
                {p.label_zh}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ===== 模型服务 Section ===== */
function ProvidersSection({ state, setFlash }) {
  const [addMode, setAddMode] = useState(null); // null | "pick" | {preset} —— 只管添加流程
  const [editing, setEditing] = useState({});   // { [provider_id]: true } —— 每张卡独立原位展开编辑
  const providers = state.overview?.providers || {};
  const ids = Object.keys(providers);

  const openAdd = () => {
    WsAiProviders.loadPresets().catch((error) => setFlash({ tone: "err", text: errText(error, "加载预设目录失败。") }));
    setAddMode("pick");
  };
  const openEdit = (id) => {
    WsAiProviders.loadPresets().catch(() => {});
    setEditing(e => ({ ...e, [id]: true }));
  };
  const closeEdit = (id) => setEditing(({ [id]: _closed, ...rest }) => rest);

  return (
    <Section title="模型服务" desc="接入各家模型或第三方中转;密钥加密存于后端,本机只留管理令牌。">
      {ids.length === 0 && (
        <div className="set-readonly" style={{ padding: "6px 0" }}>
          还没有接入任何模型服务。点「添加模型服务」,选择厂商即可开始。
        </div>
      )}
      {ids.map(id => editing[id] ? (
        <ProviderForm key={id} state={state} preset={null} editingId={id}
          onDone={() => closeEdit(id)} setFlash={setFlash} />
      ) : (
        <ProviderCard key={id} id={id} provider={providers[id]} state={state}
          onEdit={openEdit} setFlash={setFlash} />
      ))}
      {addMode === null && (
        <div><button className="btn btn-primary" onClick={openAdd}><I.Plus size={14} /> 添加模型服务</button></div>
      )}
      {addMode === "pick" && <PresetPicker state={state} onPick={(p) => setAddMode({ preset: p })} onClose={() => setAddMode(null)} />}
      {addMode && addMode.preset && (
        <ProviderForm state={state} preset={addMode.preset} editingId={null} onDone={() => setAddMode(null)} setFlash={setFlash} />
      )}
    </Section>
  );
}

/* ===== 模型分工(角色槽位) ===== */
function RoleSlotsSection({ state, setFlash }) {
  const overview = state.overview || {};
  const slots = overview.role_slots || [];
  const providers = overview.providers || {};
  const readyIds = Object.keys(providers).filter(id => {
    const p = providers[id];
    if (p.enabled === false) return false;
    return p.credential_mode === "none" || p.secret?.configured;
  });
  const [draft, setDraft] = useState({});
  const busy = state.busy["role-routes"];

  const slotValue = (slot) => draft[slot.slot_id] || {
    provider_id: slot.current?.provider_id || "",
    model: slot.current?.model || "",
  };

  const apply = () => {
    const assignments = {};
    slots.forEach(slot => {
      const v = slotValue(slot);
      if (v.provider_id && v.model) assignments[slot.slot_id] = { provider_id: v.provider_id, model: v.model };
    });
    if (!Object.keys(assignments).length) {
      setFlash({ tone: "err", text: "先为至少一个分工选择服务和模型。" });
      return;
    }
    if (!window.confirm("应用分工会覆写这些槽位内全部节点的路由(包括你在高级路由里的单独调整)。继续?")) return;
    WsAiProviders.saveRoleRoutes(assignments, true)
      .then(() => { setDraft({}); setFlash({ tone: "ok", text: "分工已应用,对应节点的路由已经生效。" }); })
      .catch((error) => setFlash({ tone: "err", text: errText(error, "应用分工失败。") }));
  };

  return (
    <Section title="模型分工" desc="不用逐个节点配置——按写作场景把模型分成三班岗,应用后立即生效。">
      {slots.map(slot => {
        const v = slotValue(slot);
        const models = v.provider_id ? (providers[v.provider_id]?.models || []) : [];
        const mixed = slot.current?.mixed;
        return (
          <Row key={slot.slot_id} label={slot.label_zh}
            hint={`${slot.description_zh} · ${slot.node_ids.length} 个节点${mixed ? " · 当前为自定义混合路由" : ""}`}>
            <select className="select" value={v.provider_id}
              onChange={(e) => setDraft(d => ({ ...d, [slot.slot_id]: { provider_id: e.target.value, model: "" } }))}>
              <option value="">选择服务…</option>
              {readyIds.map(id => <option key={id} value={id}>{id}</option>)}
            </select>
            <select className="select" value={v.model} disabled={!v.provider_id}
              onChange={(e) => setDraft(d => ({ ...d, [slot.slot_id]: { provider_id: v.provider_id, model: e.target.value } }))}>
              <option value="">选择模型…</option>
              {models.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </Row>
        );
      })}
      <div style={{ marginTop: 8 }}>
        <button className="btn btn-primary" disabled={busy} onClick={apply}>{busy ? "应用中…" : "应用分工"}</button>
      </div>
    </Section>
  );
}

/* ===== 高级路由(全节点矩阵,折叠) ===== */
function AdvancedRoutes({ state, setFlash }) {
  const overview = state.overview || {};
  const nodeRoutes = overview.node_routes || {};
  const providers = overview.providers || {};
  const missing = overview.missing_active_routes || [];
  const groups = useMemo(() => {
    const map = new Map();
    Object.values(nodeRoutes)
      .filter(r => r.status === "active" && r.requires_llm !== false)
      .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
      .forEach(r => {
        if (!map.has(r.group)) map.set(r.group, []);
        map.get(r.group).push(r);
      });
    return Array.from(map.entries());
  }, [nodeRoutes]);

  return (
    <details style={{ marginTop: 4 }}>
      <summary style={{ cursor: "pointer", fontSize: 13.5, color: "var(--ink-2)", padding: "6px 0" }}>
        高级路由 — 按节点查看与微调({Object.keys(nodeRoutes).length} 个节点{missing.length ? ` · ${missing.length} 个缺失` : ""})
      </summary>
      <div style={{ padding: "8px 0" }}>
        {missing.length > 0 && (
          <div className="set-readonly" style={{ marginBottom: 8 }}>
            有 {missing.length} 个节点还没有指派模型。
            <button className="btn btn-ghost" style={{ marginLeft: 8 }} disabled={state.busy["sync-missing"]}
              onClick={() => WsAiProviders.syncMissing({})
                .then((r) => setFlash({ tone: "ok", text: `已用默认服务补齐 ${r.synced_node_ids?.length ?? 0} 个节点。` }))
                .catch((error) => setFlash({ tone: "err", text: errText(error, "补齐路由失败。") }))}>
              用默认服务补齐
            </button>
          </div>
        )}
        {groups.map(([group, routes]) => (
          <div key={group} style={{ marginBottom: 10 }}>
            <div className="set-row-hint" style={{ margin: "4px 0" }}>{group}</div>
            {routes.map(route => (
              <div key={route.node_id} style={{
                display: "flex", alignItems: "center", gap: 8, padding: "4px 0",
                borderBottom: "1px solid var(--line-1, rgba(0,0,0,0.05))", fontSize: 12.5,
              }}>
                <span title={route.readiness_reason || ""} style={{
                  width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
                  background: route.ready ? "var(--sage, #5a7d5a)" : "var(--rose, #b04a4a)",
                }} />
                <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  title={route.node_id}>{route.label || route.node_id}</span>
                <span className="set-readonly" style={{ flexShrink: 0 }}>
                  {route.provider_id ? `${route.provider_id} / ${route.model}` : "未指派"}
                </span>
              </div>
            ))}
          </div>
        ))}
        <div className="set-row-hint">
          逐节点的精细编辑(温度、输出预算、回退链)在高级模式的「系统配置」里;这里改分工即可覆盖绝大多数场景。
          {Object.keys(providers).length === 0 ? " 先接入一个模型服务。" : ""}
        </div>
      </div>
    </details>
  );
}

/* ===== AI 行为(本地偏好,沿用原型) =====
   2026-09 W7：删除「允许引用参考画像」「复刻检查严格度」两个开关——它们只写 localStorage
   （aiAllowRef / aiStrict），没有任何生成 / 注入 / 校验代码读取，属会误导作者的死开关。
   参考画像是否生效由风格参考页的绑定决定；抄袭检查阈值是后端配置。 */
function BehaviorSection() {
  const [candN, setCandN] = usePref("aiCandN", 3);
  return (
    <Section title="AI 行为">
      <Row label="生成候选数" hint="每次「再生」产出的候选条数。">
        <Segmented options={[{ value: 2, label: "2" }, { value: 3, label: "3" }, { value: 5, label: "5" }]} value={candN} onChange={setCandN} />
      </Row>
    </Section>
  );
}

/* ===== 主入口 ===== */
function AISettings() {
  const state = useAiProviders();
  const [flash, setFlash] = useState(null);

  useEffect(() => {
    if (!state.loaded && !state.loading) {
      WsAiProviders.refresh().catch(() => {});
      WsAiProviders.loadPresets().catch(() => {});
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (state.error && !state.overview) {
    return (
      <Section title="模型接入" desc="读取后端模型配置失败。">
        <div className="set-readonly" style={{ color: "var(--rose)" }}>{state.error.message}</div>
        <div style={{ marginTop: 8 }}>
          <button className="btn btn-primary" onClick={() => WsAiProviders.refresh().catch(() => {})}>重试</button>
        </div>
      </Section>
    );
  }
  if (!state.overview) {
    return <Section title="模型接入" desc="正在读取后端模型配置…"><div className="set-readonly">加载中…</div></Section>;
  }

  return (
    <>
      <Flash flash={flash} onClose={() => setFlash(null)} />
      <StatusSection state={state} />
      <ProvidersSection state={state} setFlash={setFlash} />
      <RoleSlotsSection state={state} setFlash={setFlash} />
      <AdvancedRoutes state={state} setFlash={setFlash} />
      <BehaviorSection />
    </>
  );
}

export { AISettings };
