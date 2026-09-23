import React from "react";
import { WsWorks } from "./ws-works.jsx";
import { fidFinalsSummary } from "./ws-fidelity-model.js";
import { fidLoadProject, fidProject, useFidelityStore } from "./ws-fidelity-store.js";
import { FidelityBadge } from "./ws-fidelity-ui.jsx";

/* ==========================================================
   成稿中心 · 像不像（2026-09-23 风格参考 v3 · P6b）
   每一场最新的终稿读数（GET /api/v1/projects/{id}/style-fidelity 的 scene_finals）：结构页签的场景行、正文里每一场的
   场头各挂一个小角标（「作者范围内 · 第 42 位」/「超出范围 · 第 96 位」/「量不准」）；页头一句「12 场终稿里 9 场在
   作者范围内」。作品没用参考书的文风时什么也不挂。整部作品的走势在风格参考的文风画像页。
   ========================================================== */

export function useManuFidelity() {
  useFidelityStore();
  const workId = WsWorks.activeId();
  const live = workId && workId !== "__loading__" ? workId : null;
  React.useEffect(() => { if (live) fidLoadProject(live, { force: true }); }, [live]);
  const entry = live ? fidProject(live) : null;
  const data = entry && entry.data;
  if (!data || !data.bound) return { finals: {}, summary: null };
  const finals = data.scene_finals && typeof data.scene_finals === "object" ? data.scene_finals : {};
  return { finals, summary: fidFinalsSummary(finals) };
}

export function ManuFidelityBadge({ finals, sceneId, testId = "ms-scene-fidelity" }) {
  if (!finals || !sceneId || !finals[sceneId]) return null;
  return <FidelityBadge final={finals[sceneId]} testId={testId} />;
}
