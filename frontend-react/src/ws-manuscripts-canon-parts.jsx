import React from "react";
import { I } from "./icons.jsx";
import { Spinner } from "./ws-ui.jsx";
import { CANON_EVENT_LABELS, CANON_EXTRACTION_OUTCOMES, CANON_EXTRACTION_REASONS, CANON_STATUS_LABELS } from "./labels/canon.js";

/* ==========================================================
   正史审核台的零件（从 ws-manuscripts-canon.jsx 拆出，2026-10）
   · CanonSceneCard —— 一场：状态、提取结果（可重新提取）、候选事实逐条裁决、整场通读确认；
   · CanonCandidate —— 一条候选事实：证据、实体消歧、接受 / 驳回；
   · CanonManualEntry —— 模型漏掉的事实从终稿证据手工补录（表单状态由正史台持有：换章只清「选哪一场」）。
   动作都由正史台（ManuCanon）给：这里只画。
   ========================================================== */

const NEEDS_EXTRACTION = ["pending_extraction", "degraded"];

function sceneChip(scene, pendingCount) {
  if (scene.complete) return "已提交";
  if (scene.status === "missing_final") return "等终稿";
  if (pendingCount) return `${pendingCount} 条待裁决`;
  return NEEDS_EXTRACTION.includes(scene.status) ? "待提取" : "待通读确认";
}

/* busyKey：正在跑的动作（同一时间只有一个）；entityChoice：候选 id → 选定的实体；verifyNote / onVerifyNote：本场的确认说明 */
export function CanonSceneCard({ scene, busyKey, entityChoice, onSelectEntity, onDecide, onExtract, verifyNote, onVerifyNote, onVerify }) {
  const pending = (scene.candidates || []).filter((candidate) => candidate.status === "pending");
  const extraction = scene.extraction || {};
  const needsExtraction = NEEDS_EXTRACTION.includes(scene.status);
  return (
    <section className={`ms-canon-scene status-${scene.status}`} data-testid="canon-scene-card">
      <header>
        <div>
          <span className="ms-canon-scene-no">{scene.scene_seq ? `第 ${scene.scene_seq} 场` : "未编号"}</span>
          <strong>{CANON_STATUS_LABELS[scene.status] || "状态未知"}</strong>
        </div>
        <span className={`ms-canon-status ${scene.complete ? "is-complete" : "is-pending"}`}>
          {scene.complete ? <I.CheckCircle size={13} /> : <I.Clock size={13} />}
          {sceneChip(scene, pending.length)}
        </span>
      </header>

      {!scene.complete && (extraction.extraction_outcome || needsExtraction) && (
        <div className="ms-canon-extract-note">
          <span>
            <span title={[extraction.extraction_outcome, extraction.extraction_reason].filter(Boolean).join(" / ") || undefined}>
              {CANON_EXTRACTION_OUTCOMES[extraction.extraction_outcome || "not_invoked"] || "提取状态未知"}
              {extraction.extraction_reason && CANON_EXTRACTION_REASONS[extraction.extraction_reason] ? `（${CANON_EXTRACTION_REASONS[extraction.extraction_reason]}）` : ""}
            </span>
            {extraction.requires_empty_confirmation
              ? <span>模型没发现持久变化，仍需你通读确认。</span>
              : extraction.requires_scene_confirmation
                ? <span>候选裁决完成后，还要通读确认没有漏项。</span>
                : null}
          </span>
          {needsExtraction && (
            <button type="button" className="btn btn-quiet btn-sm" data-testid="canon-scene-extract" disabled={Boolean(busyKey)} onClick={() => onExtract(scene)}>
              {busyKey === `extract:${scene.scene_id}` ? <Spinner size={12} /> : <I.Sparkles size={12} />}
              用模型重新提取
            </button>
          )}
        </div>
      )}

      <div className="ms-canon-candidates">
        {(scene.candidates || []).map((candidate) => (
          <CanonCandidate key={candidate.candidate_id} candidate={candidate} busy={Boolean(busyKey)}
            selected={entityChoice[candidate.candidate_id] || ""}
            onSelect={(value) => onSelectEntity(candidate.candidate_id, value)}
            onDecide={(action) => onDecide(scene, candidate, action)} />
        ))}
      </div>

      {!scene.complete && pending.length === 0 && scene.final_scene_row_id && (
        <div className="ms-canon-verify">
          <label htmlFor={`canon-note-${scene.scene_id}`}>通读确认</label>
          <textarea id={`canon-note-${scene.scene_id}`} value={verifyNote || ""}
            onChange={(event) => onVerifyNote(scene.scene_id, event.target.value)}
            placeholder="例如：已通读本场，没有新的持久状态变化；人物伤势与上一场一致。" />
          <button type="button" className="btn btn-accent btn-sm" data-testid="canon-scene-verify" disabled={Boolean(busyKey)} onClick={() => onVerify(scene)}><I.ShieldCheck size={13} /> 确认本场正史</button>
        </div>
      )}
    </section>
  );
}

export function CanonCandidate({ candidate, busy, selected, onSelect, onDecide }) {
  const needsEntity = ["ambiguous", "unresolved"].includes(candidate.entity_resolution_status);
  const evidenceGrounded = !candidate.evidence || candidate.evidence.grounded !== false;
  const canAccept = evidenceGrounded && (!needsEntity || Boolean(selected));
  const pending = candidate.status === "pending";
  return (
    <article className={`ms-canon-candidate is-${candidate.status}`} data-testid="canon-candidate">
      <div className="ms-canon-candidate-top">
        <span>{CANON_EVENT_LABELS[candidate.event_type] || candidate.event_type}</span>
        <span>{pending ? "候选" : candidate.status === "accepted" ? "已接受 · 待整场确认" : "已驳回"}</span>
      </div>
      <div className="ms-canon-fact">
        <b>{candidate.raw_entity_ref}</b>
        <span>{candidate.fact_key}</span>
        <I.ArrowRight size={13} />
        <strong>{candidate.fact_value}</strong>
      </div>
      <blockquote>“{(candidate.evidence && candidate.evidence.text) || "无证据摘录"}”</blockquote>
      {!evidenceGrounded && <div className="ms-canon-evidence-warning"><I.AlertTriangle size={12} /> 这段引文不在当前终稿中，只能驳回后重新补录。</div>}
      {pending && needsEntity && (
        <label className="ms-canon-entity">
          <span>{candidate.entity_resolution_status === "ambiguous" ? "同名/别名冲突，请指定实体" : "未识别实体；可驳回后用规范名称补录"}</span>
          {(candidate.entity_options || []).length > 0 && (
            <select value={selected} onChange={(event) => onSelect(event.target.value)}>
              <option value="">选择正史实体…</option>
              {candidate.entity_options.map((option) => <option value={option.entity_id} key={option.entity_id}>{option.label}</option>)}
            </select>
          )}
        </label>
      )}
      {pending && (
        <div className="ms-canon-actions">
          <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onDecide("reject")}>驳回</button>
          <button type="button" className="btn btn-accent btn-sm" data-testid="canon-candidate-accept" disabled={busy || !canAccept} onClick={() => onDecide("accept")}><I.Check size={13} /> 接受此事实</button>
        </div>
      )}
    </article>
  );
}

/* scenes：还能补录的场（有终稿、还没整场确认）；manual：表单字段；setField(key) 返回该字段的 onChange */
export function CanonManualEntry({ scenes, sceneId, onSceneId, manual, setField, busy, onAdd }) {
  if (!scenes.length) return null;
  return (
    <details className="ms-canon-manual">
      <summary><I.Plus size={13} /> 模型漏掉了事实？从终稿证据手工补录</summary>
      <div className="ms-canon-manual-grid">
        <label><span>场景</span><select value={sceneId} onChange={(event) => onSceneId(event.target.value)}><option value="">选择场景…</option>{scenes.map((scene) => <option key={scene.scene_id} value={scene.scene_id}>第 {scene.scene_seq} 场</option>)}</select></label>
        <label><span>类型</span><select value={manual.event_type} onChange={setField("event_type")}>{Object.entries(CANON_EVENT_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label><span>实体规范名</span><input value={manual.raw_entity_ref} onChange={setField("raw_entity_ref")} placeholder="如：林远" /></label>
        <label><span>事实键</span><input value={manual.fact_key} onChange={setField("fact_key")} placeholder="如：injury" /></label>
        <label className="is-wide"><span>事实值</span><input value={manual.fact_value} onChange={setField("fact_value")} placeholder="如：右臂骨折，需要固定六周" /></label>
        <label className="is-wide"><span>终稿原文证据（必须逐字存在）</span><textarea value={manual.evidence_text} onChange={setField("evidence_text")} placeholder="粘贴当前终稿中的原句" /></label>
      </div>
      <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={onAdd}>加入待审核候选</button>
    </details>
  );
}
