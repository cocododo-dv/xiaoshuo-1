import React from "react";
import { apiGet, apiPatch } from "./lib/client.js";
import { createRevisionedDocs, useRevisionedDoc } from "./lib/revisioned-doc.js";
import { sceneApiId } from "./ws-scene-id.js";
import { wsKey } from "./ws-works.jsx";

/* ==========================================================
   本场笔记（2026-09-21 从 ws-writer.jsx 拆出）
   只跟这一场有关的提醒、伏笔、待回收。存服务器（/api/v1/scenes/{id}/author-notes，
   带修订号；冲突时停下来让作者决定），本机留一份缓存（wr-notes:*）和「还没同步」的标记
   （wr-notes-pending:*）。写穿、串行保存、离场冲刷、回来先等离场那条链落地，都在
   lib/revisioned-doc.js：换场时旧场景排队中的保存照旧存回旧场景（带旧场景自己的修订号），
   不会落到新场景上；防抖还没到点的改动在离开时立刻存。ESM 模块，不写 window。
   ========================================================== */

/* 目录 sid → 后端 scene id。解析到了就记住（同一场连着存几次，不必每次再等目录），解析不到不记。 */
const backendIds = new Map();
async function notesSceneId(scene) {
  if (backendIds.has(scene)) return backendIds.get(scene);
  const sceneId = await sceneApiId(scene);
  if (sceneId) backendIds.set(scene, sceneId);
  return sceneId;
}

const sceneNotes = createRevisionedDocs({
  cacheKey: (scene) => wsKey("wr-notes:" + scene),
  pendingKey: (scene) => wsKey("wr-notes-pending:" + scene),
  async load(scene) {
    const sceneId = await notesSceneId(scene);
    if (!sceneId) return null;   // 这一场还没同步到服务器：先用本机那一份
    const data = await apiGet(`/api/v1/scenes/${sceneId}/author-notes`);
    return { value: (data && data.notes) || "", revision: data && data.revision_no };
  },
  async save(scene, notes, baseRevision) {
    const sceneId = await notesSceneId(scene);
    if (!sceneId) throw Object.assign(new Error("场景尚未同步到服务器"), { code: "SCENE_NOT_READY" });
    const data = await apiPatch(`/api/v1/scenes/${sceneId}/author-notes`, { notes, base_revision_no: baseRevision });
    return { revision: data && data.revision_no };
  },
  async reloadRevision(scene) {
    const sceneId = await notesSceneId(scene);
    if (!sceneId) return null;
    const current = await apiGet(`/api/v1/scenes/${sceneId}/author-notes`);
    return current && current.revision_no;
  },
  isConflict: (error) => !!(error && error.code === "SCENE_AUTHOR_NOTES_CONFLICT"),
});

const STATUS_TEXT = {
  loading: "读取服务器…",
  saving: "保存中…",
  saved: "已存服务器",
  local: "未同步（已留本机）",
  conflict: "与其他设备冲突",
  error: "保存失败，请复制留底",
};

export function WrCtxNotes({ scene }) {
  const notes = useRevisionedDoc(sceneNotes, scene);
  const { status } = notes;
  const statusText = STATUS_TEXT[status] || "保存状态未知";
  const healthy = status === "saved";
  return (
    <>
      <div className="wr-notes-bar">
        <span className="wr-notes-cap">本场笔记</span>
        <span className={`wr-notes-state ${healthy ? "" : "saving"}`}>
          <span className="wr-notes-dot" />{statusText}
          {(status === "local" || status === "conflict" || status === "error") && (
            <button type="button" className="btn btn-quiet btn-sm" onClick={notes.retry}>重试</button>
          )}
        </span>
      </div>
      <textarea className="wr-notes" value={notes.value} onChange={(e) => notes.edit(e.target.value)} placeholder="只跟这一场有关的提醒、伏笔、待回收……（自动保存到服务器，按场独立）" />
    </>
  );
}
