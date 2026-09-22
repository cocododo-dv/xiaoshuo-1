import React from "react";
import { I } from "./icons.jsx";
import { WsManuStore } from "./ws-manuscripts-store.jsx";
import { Notice, Spinner } from "./ws-ui.jsx";

/* ==========================================================
   正史审核台：AI 从终稿里抽出的只是候选事实，作者逐条裁决、再整场通读确认后，
   人物状态 / 知识 / 关系 / 时间线才进入后续写作的上下文。
   数据全部来自章节聚合快照的 canonContinuity（WsManuStore），动作写回服务端后重拉同章。
   ========================================================== */

const { useEffect, useState } = React;

const CANON_EVENT_LABELS = {
  character_state: "人物状态",
  character_learns: "人物获知",
  location_change: "位置变化",
  relation_change: "关系变化",
  item_change: "物品变化",
  foreshadow_plant: "伏笔埋设",
  foreshadow_reinforce: "伏笔强化",
  foreshadow_resolve: "伏笔兑现",
};

/* 提取结果与原因（后端 canon_continuity / prose_event_extractor 的枚举）→ 作者读得懂的话 */
const CANON_EXTRACTION_OUTCOMES = {
  not_invoked: "还没提取",
  completed_events: "已提取出候选事实",
  completed_empty: "模型没找到持久变化",
  facts_unchanged: "事实没有变化",
  parse_failed: "模型的回复无法解析",
  provider_failed: "模型调用失败",
  rejected_before_dispatch: "请求在发出前被拦下",
};
const CANON_EXTRACTION_REASONS = {
  awaiting_extraction: "等待提取",
  runner_disabled: "自动提取没有开启",
  feature_disabled: "自动提取没有开启",
  offline_unsupported: "当前没有可用的模型",
  pre_dispatch_rejection: "额度或预算拦下了请求",
  provider_call_failed: "模型服务报错",
  invalid_llm_response: "回复格式不对",
};

const CANON_STATUS_LABELS = {
  synced: "已提交正史",
  pending_review: "等待作者裁决",
  pending_extraction: "等待提取或人工确认",
  degraded: "自动提取降级",
  missing_final: "缺少终稿",
};

const NEEDS_EXTRACTION = ["pending_extraction", "degraded"];

function sceneChip(scene, pendingCount) {
  if (scene.complete) return "已提交";
  if (scene.status === "missing_final") return "等终稿";
  if (pendingCount) return `${pendingCount} 条待裁决`;
  return NEEDS_EXTRACTION.includes(scene.status) ? "待提取" : "待通读确认";
}

const EMPTY_MANUAL = { event_type: "character_state", raw_entity_ref: "", fact_key: "", fact_value: "", evidence_text: "" };

function ManuCanon({ projectId, chapterId, canonical, onChanged }) {
  const [busyKey, setBusyKey] = useState("");
  const [feedback, setFeedback] = useState({ error: "", note: "" });
  const [entityChoice, setEntityChoice] = useState({});
  const [verifyNotes, setVerifyNotes] = useState({});
  const [manualSceneId, setManualSceneId] = useState("");
  const [manual, setManual] = useState(EMPTY_MANUAL);

  useEffect(() => {
    setBusyKey("");
    setFeedback({ error: "", note: "" });
    setEntityChoice({});
    setVerifyNotes({});
    setManualSceneId("");
  }, [chapterId]);

  if (!canonical || canonical.status === "idle" || canonical.status === "loading") {
    return <div className="ms-canon-empty" role="status"><Spinner size={15} /> 正在读取正史提交状态…</div>;
  }
  if (canonical.status === "error") {
    return <div className="ms-canon-empty is-error"><I.AlertTriangle size={17} /> {(canonical.error && canonical.error.message) || "正史状态加载失败。"}</div>;
  }

  const canon = canonical.body && canonical.body.canonContinuity;
  if (!canon) {
    return <div className="ms-canon-empty is-error"><I.AlertTriangle size={17} /> 服务端没有返回正史核验结果，终稿流转已安全暂停。</div>;
  }

  const scenes = canon.scenes || [];
  const run = async (key, action, successNote) => {
    if (busyKey) return;
    setBusyKey(key);
    setFeedback({ error: "", note: "" });
    try {
      await action();
      setFeedback({ error: "", note: successNote });
      if (onChanged) onChanged();
    } catch (e) {
      setFeedback({ error: (e && e.message) || "正史操作失败。", note: "" });
    } finally {
      setBusyKey("");
    }
  };

  const decide = (scene, candidate, action) => run(
    `candidate:${candidate.candidate_id}`,
    () => WsManuStore.decideCanonCandidate(projectId, chapterId, candidate.candidate_id, {
      action,
      selected_entity_id: entityChoice[candidate.candidate_id] || null,
      expected_final_scene_row_id: scene.final_scene_row_id,
    }),
    action === "accept" ? "事实已接受；完成整场确认后才会进入正史。" : "候选已驳回，不会进入后续写作上下文。",
  );

  const verify = (scene) => {
    const note = String(verifyNotes[scene.scene_id] || "").trim();
    if (!note) {
      setFeedback({ error: "请写明你核对了什么，再确认本场正史。", note: "" });
      return undefined;
    }
    return run(
      `verify:${scene.scene_id}`,
      () => WsManuStore.verifySceneCanon(projectId, chapterId, scene.scene_id, {
        note,
        expected_final_scene_row_id: scene.final_scene_row_id,
      }),
      `第 ${scene.scene_seq} 场正史已由作者确认。`,
    );
  };

  const extract = (scene) => run(
    `extract:${scene.scene_id}`,
    () => WsManuStore.extractSceneCanon(projectId, chapterId, scene.scene_id),
    `第 ${scene.scene_seq} 场已重新提取，请逐条裁决。`,
  );

  const addManual = () => {
    if (!manualSceneId) {
      setFeedback({ error: "请先选择要补录事实的场景。", note: "" });
      return undefined;
    }
    const payload = Object.fromEntries(Object.entries(manual).map(([key, value]) => [key, String(value || "").trim()]));
    if (!payload.raw_entity_ref || !payload.fact_key || !payload.fact_value || !payload.evidence_text) {
      setFeedback({ error: "实体、事实键、事实值和正文证据都必须填写。", note: "" });
      return undefined;
    }
    return run(
      `manual:${manualSceneId}`,
      () => WsManuStore.createCanonCandidate(projectId, chapterId, manualSceneId, payload),
      "人工事实候选已加入，请继续接受或驳回。",
    );
  };
  const setManualField = (key) => (event) => setManual((current) => ({ ...current, [key]: event.target.value }));
  const manualScenes = scenes.filter((scene) => scene.final_scene_row_id && !scene.complete);

  return (
    <div className="ms-canon" data-testid="manuscript-canon-panel">
      <section className={`ms-canon-overview ${canon.complete ? "is-complete" : "is-pending"}`}>
        <div className="ms-canon-seal">{canon.complete ? <I.ShieldCheck size={24} /> : <I.Database size={22} />}</div>
        <div>
          <h2 className="text-serif">{canon.complete ? "本章事实已经提交" : "本章仍有事实需要你落槌"}</h2>
          <p>AI 只能从终稿提出候选；只有你逐条裁决并完成整场确认后，人物状态、知识、关系与时间线才会原子进入后续章节。</p>
        </div>
        <div className="ms-canon-counts">
          <span><b>{canon.synced_scene_count || 0}</b> / {canon.scene_count || 0} 场已提交</span>
          <span><b>{canon.pending_candidate_count || 0}</b> 条待裁决</span>
        </div>
      </section>

      {(feedback.error || feedback.note) && (
        <Notice tone={feedback.error ? "danger" : "ok"} className="ms-canon-feedback">{feedback.error || feedback.note}</Notice>
      )}

      <div className="ms-canon-scenes">
        {scenes.map((scene) => {
          const pending = (scene.candidates || []).filter((candidate) => candidate.status === "pending");
          const extraction = scene.extraction || {};
          const needsExtraction = NEEDS_EXTRACTION.includes(scene.status);
          return (
            <section className={`ms-canon-scene status-${scene.status}`} key={scene.scene_id} data-testid="canon-scene-card">
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
                    <button type="button" className="btn btn-quiet btn-sm" data-testid="canon-scene-extract" disabled={Boolean(busyKey)} onClick={() => extract(scene)}>
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
                    onSelect={(value) => setEntityChoice((current) => ({ ...current, [candidate.candidate_id]: value }))}
                    onDecide={(action) => decide(scene, candidate, action)} />
                ))}
              </div>

              {!scene.complete && pending.length === 0 && scene.final_scene_row_id && (
                <div className="ms-canon-verify">
                  <label htmlFor={`canon-note-${scene.scene_id}`}>通读确认</label>
                  <textarea id={`canon-note-${scene.scene_id}`} value={verifyNotes[scene.scene_id] || ""}
                    onChange={(event) => setVerifyNotes((current) => ({ ...current, [scene.scene_id]: event.target.value }))}
                    placeholder="例如：已通读本场，没有新的持久状态变化；人物伤势与上一场一致。" />
                  <button type="button" className="btn btn-accent btn-sm" data-testid="canon-scene-verify" disabled={Boolean(busyKey)} onClick={() => verify(scene)}><I.ShieldCheck size={13} /> 确认本场正史</button>
                </div>
              )}
            </section>
          );
        })}
      </div>

      {manualScenes.length > 0 && (
        <details className="ms-canon-manual">
          <summary><I.Plus size={13} /> 模型漏掉了事实？从终稿证据手工补录</summary>
          <div className="ms-canon-manual-grid">
            <label><span>场景</span><select value={manualSceneId} onChange={(event) => setManualSceneId(event.target.value)}><option value="">选择场景…</option>{manualScenes.map((scene) => <option key={scene.scene_id} value={scene.scene_id}>第 {scene.scene_seq} 场</option>)}</select></label>
            <label><span>类型</span><select value={manual.event_type} onChange={setManualField("event_type")}>{Object.entries(CANON_EVENT_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
            <label><span>实体规范名</span><input value={manual.raw_entity_ref} onChange={setManualField("raw_entity_ref")} placeholder="如：林远" /></label>
            <label><span>事实键</span><input value={manual.fact_key} onChange={setManualField("fact_key")} placeholder="如：injury" /></label>
            <label className="is-wide"><span>事实值</span><input value={manual.fact_value} onChange={setManualField("fact_value")} placeholder="如：右臂骨折，需要固定六周" /></label>
            <label className="is-wide"><span>终稿原文证据（必须逐字存在）</span><textarea value={manual.evidence_text} onChange={setManualField("evidence_text")} placeholder="粘贴当前终稿中的原句" /></label>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" disabled={Boolean(busyKey)} onClick={addManual}>加入待审核候选</button>
        </details>
      )}
    </div>
  );
}

function CanonCandidate({ candidate, busy, selected, onSelect, onDecide }) {
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

export { ManuCanon };
