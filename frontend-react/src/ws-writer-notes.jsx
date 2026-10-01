import React from "react";
import { apiGet, apiPatch } from "./lib/client.js";
import { createRevisionedDocs, useRevisionedDoc } from "./lib/revisioned-doc.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { sceneApiId } from "./ws-scene-id.js";
import { wsKey } from "./ws-works.jsx";

/* ==========================================================
   本场笔记（2026-09-21 从 ws-writer.jsx 拆出）
   只跟这一场有关的提醒、伏笔、待回收。存服务器（/api/v1/scenes/{id}/author-notes，
   带修订号；冲突时停下来让作者决定），本机留一份缓存（wr-notes:*）和「还没同步」的标记
   （wr-notes-pending:*）。写穿、串行保存、离场冲刷、回来先等离场那条链落地，都在
   lib/revisioned-doc.js：换场时旧场景排队中的保存照旧存回旧场景（带旧场景自己的修订号），
   不会落到新场景上；防抖还没到点的改动在离开时立刻存。
   乐观新建的场换了名字（临时 sid → 稳定的 scene_id）：本机缓存与未同步标记跟到新名字下——开着的这一份换名时由
   useRevisionedDoc 的 renamedFrom 搬（先等旧名字那一份没存完的保存落地），没开着的由目录重读时
   ws-writer-scene-keys.js 调 wrNotesFollowRename 搬。过去它们留在临时 sid 的键下，新建那几秒写的笔记从此没人读。
   ESM 模块，不写 window。
   ========================================================== */

/* 目录给这一场换了名字吗（prev 经目录的别名解析到的就是 scene） */
function renamedTo(prev, scene) {
  if (!prev || !scene || prev === scene) return false;
  try {
    const hit = WsCatalog.sceneById(prev);
    return !!(hit && hit.scene && hit.scene.sid === scene);
  } catch (e) {
    return false;
  }
}

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

/* 没开着的那一份换了名字：本机这一层搬到新名字下（开着的返回 false，由它自己换名时搬） */
export function wrNotesFollowRename(from, to) {
  return sceneNotes.rename(from, to);
}

export function WrCtxNotes({ scene }) {
  const prevRef = React.useRef(scene);
  const renamedFrom = renamedTo(prevRef.current, scene) ? prevRef.current : null;
  React.useEffect(() => { prevRef.current = scene; }, [scene]);
  const notes = useRevisionedDoc(sceneNotes, scene, { renamedFrom });
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
