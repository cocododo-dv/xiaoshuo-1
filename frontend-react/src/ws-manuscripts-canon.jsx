import React from "react";
import { I } from "./icons.jsx";
import { WsManuStore } from "./ws-manuscripts-store.jsx";
import { Notice, Spinner } from "./ws-ui.jsx";
import { CanonManualEntry, CanonSceneCard } from "./ws-manuscripts-canon-parts.jsx";

/* ==========================================================
   正史审核台：AI 从终稿里抽出的只是候选事实，作者逐条裁决、再整场通读确认后，
   人物状态 / 知识 / 关系 / 时间线才进入后续写作的上下文。
   数据全部来自章节聚合快照的 canonContinuity（WsManuStore），动作写回服务端后重拉同章。
   这里管状态与动作；一场一张卡、一条候选、手工补录的表单在 ws-manuscripts-canon-parts.jsx，叫法在 labels/canon.js。
   ========================================================== */

const { useEffect, useState } = React;

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
        {scenes.map((scene) => (
          <CanonSceneCard key={scene.scene_id} scene={scene} busyKey={busyKey} entityChoice={entityChoice}
            onSelectEntity={(candidateId, value) => setEntityChoice((current) => ({ ...current, [candidateId]: value }))}
            onDecide={decide} onExtract={extract}
            verifyNote={verifyNotes[scene.scene_id]} onVerifyNote={(sceneId, value) => setVerifyNotes((current) => ({ ...current, [sceneId]: value }))}
            onVerify={verify} />
        ))}
      </div>

      <CanonManualEntry scenes={manualScenes} sceneId={manualSceneId} onSceneId={setManualSceneId}
        manual={manual} setField={setManualField} busy={Boolean(busyKey)} onAdd={addManual} />
    </div>
  );
}

export { ManuCanon };
