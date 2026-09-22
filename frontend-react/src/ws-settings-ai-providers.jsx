import React from "react";
import { I } from "./icons.jsx";
import { WsAiProviders } from "./ws-ai-providers.jsx";
import { errText, providerHealth } from "./ws-settings-ai-health.js";
import { Section, Row, Field, Toggle } from "./ws-settings-shared.jsx";
import { Segmented, Tag } from "./ws-ui.jsx";
import { wsConfirm } from "./ws-notify.jsx";

/* ==========================================================
   设置 · AI 模型 → 模型服务：已接入服务的卡片（启用 / 测试 / 默认 / 编辑 / 删除）、
   添加流程（先选厂商预设，再填表）和原位展开的编辑表单。
   ========================================================== */

const { useState, useEffect, useMemo, useRef } = React;

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

/* ===== 服务卡片 ===== */
function ProviderCard({ id, provider, state, onEdit, setFlash }) {
  const overview = state.overview || {};
  const isDefault = overview.default_provider_id === id;
  const catalog = state.presets?.provider_catalog || overview.provider_catalog || {};
  const typeLabel = catalog[provider.provider_type]?.label || provider.provider_type;
  const probe = state.probes[id];
  const busyProbe = state.busy[`probe:${id}`];
  const busyDelete = state.busy[`delete:${id}`];
  const health = providerHealth(provider);
  const keyHint = provider.credential_mode === "none"
    ? "免密钥"
    : (provider.secret?.configured ? `密钥 ${provider.secret.hint || "已配置"}` : "密钥未配置");
  const models = Array.isArray(provider.models) ? provider.models : [];

  const toggleEnabled = (on) => {
    WsAiProviders.saveProvider({ ...provider, provider_id: id, enabled: on })
      .catch((error) => setFlash({ tone: "err", text: errText(error, "更新服务状态失败。") }));
  };

  const remove = async () => {
    const ok = await wsConfirm({
      title: `删除服务「${id}」？`,
      body: "它的 API 密钥会一起从后端删除；还指向它的 AI 功能会变成未就绪，需要改分工或补齐路由。",
      confirmLabel: "删除服务",
      tone: "danger",
    });
    if (!ok) return;
    WsAiProviders.deleteProvider(id)
      .then((result) => {
        const orphaned = result?.orphaned_route_node_ids || [];
        setFlash(orphaned.length ? {
          tone: "ok",
          text: `服务「${id}」已删除。有 ${orphaned.length} 个 AI 功能还指向它，已标为未就绪。`,
          action: {
            label: "一键补齐路由",
            run: () => WsAiProviders.syncMissing({})
              .then((r) => setFlash({ tone: "ok", text: `已用默认服务补齐 ${r.synced_node_ids?.length ?? 0} 个 AI 功能的路由。` }))
              .catch((error) => setFlash({ tone: "err", text: errText(error, "补齐路由失败。") })),
          },
        } : { tone: "ok", text: `服务「${id}」已删除。` });
      })
      .catch((error) => setFlash({ tone: "err", text: errText(error, "删除服务失败。") }));
  };

  return (
    <div className="card-flat set-provider" id={`set-provider-${id}`}>
      <div className="set-provider-head">
        <strong className="set-provider-id">{id}</strong>
        <Tag tone="info">{typeLabel}</Tag>
        {isDefault && <Tag tone="accent">默认</Tag>}
        {health.map((h) => <Tag key={h.text} tone={h.tone} dot>{h.text}</Tag>)}
        <span className="set-provider-spacer" />
        <Toggle on={provider.enabled !== false} onChange={toggleEnabled} label="启用" showLabel />
      </div>
      <div className="set-provider-meta">
        <span className="set-provider-url">{provider.base_url}</span>
        <span className="set-provider-key">{keyHint}</span>
      </div>
      {models.length > 0 && (
        <div className="set-provider-models" title={models.join("\n")}>
          模型：{models.slice(0, 4).join("、")}{models.length > 4 ? ` 等 ${models.length} 个` : ""}
        </div>
      )}
      <div className="set-provider-actions">
        <button type="button" className="btn btn-ghost btn-sm" disabled={busyProbe}
          onClick={() => WsAiProviders.probe(id).catch(() => {})}>
          {busyProbe ? "测试中…" : "测试连接"}
        </button>
        {!isDefault && (
          <button type="button" className="btn btn-ghost btn-sm" disabled={state.busy[`default:${id}`]}
            onClick={() => WsAiProviders.setDefault(id).catch((error) => setFlash({ tone: "err", text: errText(error, "设为默认失败。") }))}>
            设为默认
          </button>
        )}
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => onEdit(id)}>编辑</button>
        <button type="button" className="btn btn-danger btn-sm" disabled={busyDelete} onClick={remove}>
          {busyDelete ? "删除中…" : "删除"}
        </button>
      </div>
      {probe && (
        <div className={`set-probe ${probe.ok ? "is-ok" : "is-err"}`} role="status">
          {probe.ok ? "✓" : "✕"} {probe.message}
          {typeof probe.latency_ms === "number" ? `（${probe.latency_ms}ms）` : ""}
        </div>
      )}
    </div>
  );
}

/* ===== 添加 / 编辑服务表单 =====
   focusField：从「接入状态」的修复按钮打开时，直接把光标放到要填的那一栏（密钥 / 模型列表）。 */
function ProviderForm({ state, preset, editingId, onDone, setFlash, focusField }) {
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
  const rootRef = useRef(null);
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const credentialChoices = editing
    ? (state.presets?.provider_catalog?.[editing.provider_type]?.credential_modes || ["api_key"])
    : preset.credential_modes;
  const models = splitModels(form.modelsText);
  const secret = editing?.secret || {};
  const keyBroken = !!(secret.configured && secret.decryptable === false);

  /* 打开时滚到视野里，并把光标放到该填的那一栏 */
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    if (typeof root.scrollIntoView === "function") root.scrollIntoView({ block: "nearest", behavior: "smooth" });
    const target = focusField === "api_key" ? root.querySelector("input[type=password]")
      : focusField === "models" ? root.querySelector("textarea") : null;
    if (target && typeof target.focus === "function") target.focus();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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
        setTestResult({ ok: true, message: `拉取到 ${available.length} 个模型${result.source === "preset" ? "（来自预设）" : ""}` });
      } else {
        setTestResult({ ok: false, message: result.message || "服务没有返回模型列表，请手动填写。" });
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
      setTestResult({ ok: result.ok, message: `${result.message}${typeof result.latency_ms === "number" ? `（${result.latency_ms}ms）` : ""}` });
    } catch (error) {
      setTestResult({ ok: false, message: errText(error, "连接测试失败。") });
    }
  };

  const save = async () => {
    if (!form.provider_id.trim()) { setTestResult({ ok: false, message: "先给这个服务起一个标识（如 my-openai）。" }); return; }
    if (!form.base_url.trim()) { setTestResult({ ok: false, message: "接口地址不能为空。" }); return; }
    setSaving(true);
    try {
      await WsAiProviders.saveProvider({ ...draftPayload(), enabled: true, models });
      const missing = WsAiProviders.state().overview?.missing_active_routes || [];
      setFlash(missing.length ? {
        tone: "ok",
        text: `服务「${form.provider_id.trim()}」已保存。还有 ${missing.length} 个 AI 功能没有指派模型。`,
        action: {
          label: "一键补齐路由",
          run: () => WsAiProviders.syncMissing({ provider_id: form.provider_id.trim() })
            .then((r) => setFlash({ tone: "ok", text: `已补齐 ${r.synced_node_ids?.length ?? 0} 个 AI 功能的路由。` }))
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

  const keyHint = !editing || !secret.configured
    ? "只加密保存在后端，不进浏览器缓存。"
    : keyBroken
      ? `存着的密钥（${secret.hint || "已配置"}）已经解不开了，请重新填写。`
      : `已配置（${secret.hint || "已配置"}）；留空则沿用。`;

  return (
    <div ref={rootRef} className="card-flat set-provider-form" id={editingId ? `set-provider-${editingId}` : undefined}>
      <div className="set-provider-form-head">
        <strong>{editing ? `编辑「${editingId}」` : `添加：${preset.label_zh}`}</strong>
        {!editing && preset.notes_zh && <span className="set-row-hint">{preset.notes_zh}</span>}
      </div>
      {!editing && (
        <Row label="服务标识" hint="本系统里给它起的名字，保存后不能改；同一家厂商可以接多个账号。">
          <Field className="input set-field" value={form.provider_id} placeholder="如 my-deepseek"
            onChange={(e) => set("provider_id", e.target.value)} />
        </Row>
      )}
      <Row label="接口地址" hint="API 的 base URL；第三方中转填它给你的地址。" stacked>
        <Field className="input set-field is-wide set-mono" value={form.base_url} spellCheck={false}
          onChange={(e) => set("base_url", e.target.value)} />
      </Row>
      {credentialChoices.length > 1 && (
        <Row label="鉴权方式" readonly>
          <Segmented label="鉴权方式" options={[
            { value: "api_key", label: "API 密钥" },
            { value: "none", label: "免密钥" },
          ]} value={form.credential_mode} onChange={(v) => set("credential_mode", v)} />
        </Row>
      )}
      {form.credential_mode === "api_key" && (
        <Row label="API 密钥" hint={keyHint} stacked>
          <Field className="input set-field is-wide set-mono" type="password" value={form.api_key} autoComplete="off"
            placeholder={keyBroken ? "重新填写 API 密钥" : (secret.configured ? "留空沿用现有密钥" : "sk-…")}
            onChange={(e) => set("api_key", e.target.value)} />
        </Row>
      )}
      <Row label="模型列表" hint="一行一个；分工和路由只能从这里选。「拉取模型」会自动获取。" stacked>
        <div className="set-models">
          <Field as="textarea" className="textarea set-models-input" rows={5} value={form.modelsText} spellCheck={false}
            onChange={(e) => set("modelsText", e.target.value)} />
          <button type="button" className="btn btn-ghost btn-sm" disabled={state.busy["draft-test"]} onClick={pullModels}>
            {state.busy["draft-test"] ? "拉取中…" : "拉取模型"}
          </button>
        </div>
      </Row>
      {testResult && (
        <div className={`set-probe ${testResult.ok ? "is-ok" : "is-err"}`} role="status">
          {testResult.ok ? "✓" : "✕"} {testResult.message}
        </div>
      )}
      <div className="set-form-actions">
        <button type="button" className="btn btn-ghost" disabled={state.busy["draft-test"]} onClick={testConnection}>连接测试</button>
        <button type="button" className="btn btn-accent" disabled={saving} onClick={save}>{saving ? "保存中…" : "保存服务"}</button>
        <button type="button" className="btn btn-quiet" onClick={onDone}>取消</button>
      </div>
    </div>
  );
}

/* ===== 预设选择（添加流程第一步） ===== */
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
    return <div className="set-readonly set-presets-loading">正在读取厂商目录…（后端没启动时会一直停在这里）</div>;
  }
  return (
    <div className="card-flat set-presets">
      <div className="set-presets-head">
        <strong>选择厂商或接入方式</strong>
        <button type="button" className="btn btn-quiet btn-sm" onClick={onClose}>取消</button>
      </div>
      {grouped.map(([category, items]) => (
        <div key={category} className="set-presets-group">
          <div className="set-presets-cat">{CATEGORY_LABELS[category] || category}</div>
          <div className="set-presets-items">
            {items.map(p => (
              <button type="button" key={p.preset_id} className="btn btn-ghost btn-sm" onClick={() => onPick(p)}
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
function ProvidersSection({ state, setFlash, editing, onEdit, onCloseEdit }) {
  const [addMode, setAddMode] = useState(null); // null | "pick" | {preset} —— 只管添加流程
  const providers = state.overview?.providers || {};
  const ids = Object.keys(providers);

  const openAdd = () => {
    WsAiProviders.loadPresets().catch((error) => setFlash({ tone: "err", text: errText(error, "加载厂商目录失败。") }));
    setAddMode("pick");
  };

  return (
    <Section title="模型服务" desc="接入各家模型或第三方中转。API 密钥加密保存在后端，浏览器里只留管理令牌。" id="set-providers">
      {ids.length === 0 && (
        <p className="set-readonly set-empty-line">还没有接入任何模型服务。点「添加模型服务」，选一家厂商就能开始。</p>
      )}
      {ids.map(id => editing[id] ? (
        <ProviderForm key={id} state={state} preset={null} editingId={id} focusField={editing[id].field}
          onDone={() => onCloseEdit(id)} setFlash={setFlash} />
      ) : (
        <ProviderCard key={id} id={id} provider={providers[id]} state={state}
          onEdit={(pid) => onEdit(pid)} setFlash={setFlash} />
      ))}
      {addMode === null && (
        <div><button type="button" className="btn btn-accent" onClick={openAdd}><I.Plus size={14} /> 添加模型服务</button></div>
      )}
      {addMode === "pick" && <PresetPicker state={state} onPick={(p) => setAddMode({ preset: p })} onClose={() => setAddMode(null)} />}
      {addMode && addMode.preset && (
        <ProviderForm state={state} preset={addMode.preset} editingId={null} onDone={() => setAddMode(null)} setFlash={setFlash} />
      )}
    </Section>
  );
}

export { ProvidersSection };
