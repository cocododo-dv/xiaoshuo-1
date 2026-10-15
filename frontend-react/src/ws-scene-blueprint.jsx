import React from "react";
import { I } from "./icons.jsx";
import { WsSceneBlueprint } from "./ws-scene-blueprint-store.js";
import { EmptyState, Notice, Tag } from "./ws-ui.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { BLUEPRINT_ORIGIN_LABELS, blueprintFieldLabel } from "./labels/blueprint.js";

/* ==========================================================
   AI 起草台 · AI 写前笔记（store 是 ws-scene-blueprint-store.js 的 WsSceneBlueprint）
   起草前 AI 对这一场「怎么演」的理解：场景蓝图 + 人物压力，起草时和设计卡一起读。蓝图分两组摆：「照第 10 步」是
   第 10 步写下的那几格落地而来，「AI 加的」是第 10 步里没有、AI 自己添的——作者一眼看出它往设计里加了什么。
   可以改成我的版本（起草优先用、构思再改也不作废）、恢复 AI 版、让 AI 重做。起草任务在跑时只读。
   editable=false：只读（起草中、看稿时）。collapsible：头部一行可收起（看稿时默认收着）。
   ========================================================== */

const { useEffect, useState, useSyncExternalStore } = React;

const SBN_FAILED = { save: "保存我的版本", restore: "恢复 AI 版", generate: "让 AI 重做" };

function useSceneBlueprint(sid) {
  useSyncExternalStore(WsSceneBlueprint.subscribe, () => WsSceneBlueprint.version());
  return WsSceneBlueprint.snapshot(sid);
}

function SbnRows({ fields }) {
  return (
    <div className="sbn-rows">
      {fields.map((f) => (
        <div className="sbn-row" key={f.key}>
          <span className="sbn-row-k">{blueprintFieldLabel(f.key)}</span>
          <span className="sbn-row-v">{f.value}</span>
        </div>
      ))}
    </div>
  );
}

function SbnGroup({ origin, fields, hint }) {
  const shown = fields.filter((f) => f.value.trim());
  if (!shown.length) return null;
  return (
    <div className={`sbn-group is-${origin}`} data-testid={`scene-blueprint-${origin}`}>
      <div className="sbn-group-head">
        <Tag tone={origin === "ai" ? "warn" : (origin === "author" ? "accent" : "neutral")}>{BLUEPRINT_ORIGIN_LABELS[origin]}</Tag>
        {hint ? <span className="sbn-hint">{hint}</span> : null}
      </div>
      <SbnRows fields={shown} />
    </div>
  );
}

function SbnEditor({ fields, values, onChange, disabled }) {
  return (
    <div className="sbn-edit">
      {fields.map((f) => (
        <label className="sbn-edit-row" key={f.key}>
          <span className="sbn-row-k">{blueprintFieldLabel(f.key)}</span>
          <textarea className="sbn-edit-text" rows={2} value={values[f.key] ?? ""} disabled={disabled}
            aria-label={`写前笔记 · ${blueprintFieldLabel(f.key)}`} placeholder="留空 = 这一格不要"
            onChange={(e) => onChange(f.key, e.target.value)} />
        </label>
      ))}
    </div>
  );
}

function SceneBlueprintNote({ sid, editable = true, collapsible = false, defaultOpen = true }) {
  const snap = useSceneBlueprint(sid);
  const [draftState, setDraftState] = useState(() => ({ sid, values: null }));
  const [open, setOpen] = useState(defaultOpen);
  const draft = draftState.sid === sid ? draftState.values : null;
  const setDraft = (values) => setDraftState({ sid, values });

  /* 每次挂上（换场、起草前后换台面）都读一次：起草时 AI 可能刚做了新的一份 */
  useEffect(() => {
    if (sid) WsSceneBlueprint.load(sid).catch(() => {});
  }, [sid]);

  if (!sid) return null;
  const data = snap.data;
  const bp = data && data.blueprint;
  const pressure = data && data.pressure;
  const busy = snap.action.busy;
  const running = !!(data && data.running);
  const canWrite = editable && !running && !busy;
  const mine = !!(bp && bp.source === "author");

  const startEdit = () => setDraft(Object.fromEntries((bp ? bp.fields : []).map((f) => [f.key, f.value])));
  const save = async () => {
    try { await WsSceneBlueprint.saveMine(sid, draft); setDraft(null); } catch (e) { /* 错误在下方就地显示 */ }
  };
  const regenerate = async () => {
    if (bp && !(await wsConfirm({
      title: "让 AI 重做写前笔记？",
      body: mine ? "你的版本会被新的一份取代。" : "新的一份会取代现在这一份。",
      confirmLabel: "重做",
    }))) return;
    try { await WsSceneBlueprint.regenerate(sid); setDraft(null); } catch (e) { /* 同上 */ }
  };
  const restore = async () => {
    if (!(await wsConfirm({
      title: "放下你的版本？",
      body: "回到你改之前 AI 的那一份；那之后构思又改过的话，这一场暂时没有笔记，下次起草时 AI 现做。",
      confirmLabel: "恢复 AI 版",
    }))) return;
    try { await WsSceneBlueprint.restoreAi(sid); } catch (e) { /* 同上 */ }
  };

  const fields = bp ? bp.fields : [];
  const err = snap.action.error;
  const folded = collapsible && !open;
  return (
    <section className={`sbn ${folded ? "is-collapsed" : ""}`} data-testid="scene-blueprint-note" aria-label="AI 写前笔记">
      <header className="sbn-head">
        <span className="sbn-title"><I.Sparkles size={13} /> AI 写前笔记</span>
        {bp ? <Tag tone={mine ? "accent" : "neutral"}>{mine ? "我的版本" : "AI 版"}</Tag> : null}
        {bp && bp.createdAt ? <span className="sbn-hint">{bp.createdAt.slice(0, 10)}</span> : null}
        {collapsible && (
          <button type="button" className="sbn-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "收起" : "展开"}
          </button>
        )}
      </header>

      {folded ? null : (<>
        <p className="sbn-sub">
          起草前 AI 对这一场怎么演的理解，起草时它和这一场的设计卡一起读。标「AI 加的」是第 10 步里没有、AI 自己添的。
        </p>
        {running && <p className="sbn-hint" role="status">起草任务在跑：等它结束再改。</p>}
        {snap.status === "loading" && !data && <p className="sbn-hint" role="status">正在读取写前笔记…</p>}
        {snap.status === "error" && (
          <Notice tone="danger">写前笔记读取失败：{(snap.error && snap.error.message) || "请稍后再试"}</Notice>
        )}
        {bp && bp.designChanged && (
          <Notice tone="warn" title="设计改过，你的版本可能过时" testId="scene-blueprint-design-changed">
            {bp.designChanged.message || "你的版本写好之后，构思或参考书又改过：它照旧用于起草；需要时改写或重做。"}
          </Notice>
        )}
        {data && !bp && (
          <EmptyState compact icon="Sparkles" title="还没有写前笔记">
            起草时 AI 会先现做一份。想先看一眼，点「先出笔记」。
          </EmptyState>
        )}

        {bp && !draft && (mine
          ? <SbnGroup origin="author" fields={fields} hint="起草优先用你的版本；构思再改也不作废" />
          : (
            <>
              <SbnGroup origin="plan" fields={fields.filter((f) => f.origin === "plan")} hint="第 10 步写下的，落成能演的样子" />
              <SbnGroup origin="ai" fields={fields.filter((f) => f.origin === "ai")} hint="第 10 步里没有，AI 自己添的" />
            </>
          ))}
        {bp && draft && (
          <SbnEditor fields={fields} values={draft} disabled={busy} onChange={(k, v) => setDraft({ ...draft, [k]: v })} />
        )}

        {bp && bp.tags.length > 0 && (
          <div className="sbn-tags">
            <span className="sbn-hint">风格选样标签</span>
            {bp.tags.map((t) => <Tag key={t} tone="neutral">{t}</Tag>)}
          </div>
        )}

        {pressure && pressure.fields.length > 0 && (
          <details className="sbn-pressure">
            <summary>人物压力 <span className="sbn-hint">AI 从人物档案推的，只读；要改就回构思改人物</span></summary>
            <SbnRows fields={pressure.fields.filter((f) => f.value.trim())} />
          </details>
        )}

        {editable && (
          <div className="sbn-actions">
            {draft ? (
              <>
                <button type="button" className="btn btn-accent btn-sm" disabled={!canWrite} onClick={save}>
                  <I.Check size={13} /> {busy && snap.action.kind === "save" ? "保存中…" : "保存我的版本"}
                </button>
                <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={() => setDraft(null)}>放弃改动</button>
              </>
            ) : (
              <>
                {bp && (
                  <button type="button" className="btn btn-ghost btn-sm" disabled={!canWrite} onClick={startEdit}>
                    <I.Edit size={13} /> {mine ? "编辑我的版本" : "改成我的版本"}
                  </button>
                )}
                {mine && (
                  <button type="button" className="btn btn-quiet btn-sm" disabled={!canWrite} onClick={restore}>
                    {busy && snap.action.kind === "restore" ? "恢复中…" : "恢复 AI 版"}
                  </button>
                )}
                <button type="button" className="btn btn-ghost btn-sm" disabled={!canWrite} onClick={regenerate}
                  title="按现在的设计重做蓝图和人物压力（会调用模型）">
                  <I.Refresh size={13} /> {busy && snap.action.kind === "generate" ? "AI 在写…" : (bp ? "重做" : "先出笔记")}
                </button>
              </>
            )}
          </div>
        )}
        {err && (
          <Notice tone="danger" testId="scene-blueprint-error">{SBN_FAILED[snap.action.kind] || "操作"}没有成功：{err.message}</Notice>
        )}
      </>)}
    </section>
  );
}

export { SceneBlueprintNote };
