import React from "react";
import { apiGet, apiPatch } from "./lib/client.js";
import { sceneApiId } from "./ws-scene-id.js";
import { wsKey } from "./ws-works.jsx";

/* ==========================================================
   本场笔记（2026-09-21 从 ws-writer.jsx 拆出）
   只跟这一场有关的提醒、伏笔、待回收。存服务器（/api/v1/scenes/{id}/author-notes，
   带修订号；冲突时停下来让作者决定），本机留一份缓存（wr-notes:*）和「还没同步」的标记
   （wr-notes-pending:*）。换场时旧场景排队中的保存照旧存回旧场景（带旧场景自己的修订号），
   不会落到新场景上；防抖还没到点的改动在离开时立刻存。ESM 模块，不写 window。
   ========================================================== */

const { useCallback, useEffect, useRef, useState } = React;

function wrNotesKey(scene) { return wsKey("wr-notes:" + scene); }
function wrNotesPendingKey(scene) { return wsKey("wr-notes-pending:" + scene); }

// 缓存键 → 离开那一场时还没存完的保存链。回到这一场先等它落地再读服务器，
// 否则会把自己刚发出去的那一次当成「别的设备改过」。
const leavingChains = new Map();

export function WrCtxNotes({ scene }) {
  const [val, setVal] = useState("");
  const [status, setStatus] = useState("loading");
  const timer = useRef(null);
  const sceneState = useRef(null);
  const valueRef = useRef("");

  // state.active 只表示「这一场还在屏幕上」（决定要不要改状态行）；离开之后排队的保存照样存回这一场。
  const persist = useCallback((value, requestedState = sceneState.current) => {
    const state = requestedState;
    if (!state) return Promise.resolve();
    state.unsaved = null;
    const version = ++state.saveVersion;
    const persistedEditVersion = state.editVersion;
    if (sceneState.current === state) setStatus("saving");
    state.chain = state.chain.then(async () => {
      try {
        const sceneId = state.backendSceneId || await sceneApiId(state.scene);
        if (!sceneId) throw Object.assign(new Error("场景尚未同步到服务器"), { code: "SCENE_NOT_READY" });
        state.backendSceneId = sceneId;
        const data = await apiPatch(`/api/v1/scenes/${sceneId}/author-notes`, {
          notes: value,
          base_revision_no: state.revision,
        });
        const remoteRevision = Number(data && data.revision_no);
        state.revision = Number.isFinite(remoteRevision)
          ? Math.max(state.revision, remoteRevision)
          : state.revision + 1;
        if (version === state.saveVersion && persistedEditVersion === state.editVersion) {
          try { localStorage.removeItem(wrNotesPendingKey(state.scene)); } catch (e) {}
        }
        if (
          sceneState.current === state
          && state.active
          && version === state.saveVersion
          && persistedEditVersion === state.editVersion
        ) setStatus("saved");
      } catch (error) {
        if (error && error.code === "SCENE_AUTHOR_NOTES_CONFLICT" && state.backendSceneId) {
          try {
            const current = await apiGet(`/api/v1/scenes/${state.backendSceneId}/author-notes`);
            const remoteRevision = Number(current && current.revision_no);
            if (Number.isFinite(remoteRevision)) state.revision = Math.max(state.revision, remoteRevision);
          } catch (e) {}
          if (sceneState.current === state && state.active && version === state.saveVersion) setStatus("conflict");
        } else if (sceneState.current === state && state.active && version === state.saveVersion) {
          setStatus("local");
        }
      }
    });
    return state.chain;
  }, []);

  useEffect(() => {
    const state = {
      scene,
      active: true,
      backendSceneId: null,
      revision: 0,
      saveVersion: 0,
      editVersion: 0,
      unsaved: null,   // 防抖还没到点的那一稿（离开时立刻存）
      chain: Promise.resolve(),
    };
    sceneState.current = state;
    let stored = null;
    let pending = false;
    try { stored = localStorage.getItem(wrNotesKey(scene)); } catch (e) {}
    try { pending = localStorage.getItem(wrNotesPendingKey(scene)) != null; } catch (e) {}
    const initial = stored != null ? stored : "";
    valueRef.current = initial;
    setVal(initial);
    setStatus(pending ? "local" : "loading");
    const loadEditVersion = state.editVersion;
    void (async () => {
      try {
        const leaving = leavingChains.get(wrNotesKey(scene));
        if (leaving) await leaving;
        if (!state.active || sceneState.current !== state) return;
        const sceneId = await sceneApiId(scene);
        if (!sceneId || !state.active || sceneState.current !== state) return;
        state.backendSceneId = sceneId;
        const data = await apiGet(`/api/v1/scenes/${sceneId}/author-notes`);
        if (!state.active || sceneState.current !== state) return;
        const remoteRevision = Number(data && data.revision_no);
        if (Number.isFinite(remoteRevision)) state.revision = Math.max(state.revision, remoteRevision);
        if (state.editVersion !== loadEditVersion) return;
        const serverValue = String((data && data.notes) || "");
        let currentStored = null;
        let currentPending = false;
        try { currentStored = localStorage.getItem(wrNotesKey(scene)); } catch (e) {}
        try { currentPending = localStorage.getItem(wrNotesPendingKey(scene)) != null; } catch (e) {}
        // 本机还有没同步上的一稿（上个会话没存上）：留着本机稿、给「重试」，不冒充冲突——
        // 只有服务端真回了 SCENE_AUTHOR_NOTES_CONFLICT 才说「与其他设备冲突」
        if (currentPending && currentStored != null && currentStored !== serverValue) {
          setStatus("local");
          return;
        }
        valueRef.current = serverValue;
        setVal(serverValue);
        try {
          localStorage.setItem(wrNotesKey(scene), serverValue);
          localStorage.removeItem(wrNotesPendingKey(scene));
        } catch (e) {}
        setStatus("saved");
      } catch (e) {
        if (state.active && sceneState.current === state && state.editVersion === loadEditVersion) {
          setStatus(stored != null ? "local" : "error");
        }
      }
    })();
    return () => {
      state.active = false;
      if (sceneState.current === state) sceneState.current = null;
      clearTimeout(timer.current);
      if (state.unsaved != null) void persist(state.unsaved, state);
      const key = wrNotesKey(scene);
      const chain = state.chain;
      leavingChains.set(key, chain);
      void chain.then(() => { if (leavingChains.get(key) === chain) leavingChains.delete(key); });
    };
  }, [scene, persist]);

  const onChange = (e) => {
    const v = e.target.value;
    const state = sceneState.current;
    if (state && state.scene === scene) {
      state.editVersion += 1;
      state.unsaved = v;
    }
    valueRef.current = v;
    setVal(v);
    setStatus("saving");
    clearTimeout(timer.current);
    try {
      localStorage.setItem(wrNotesKey(scene), v);
      localStorage.setItem(wrNotesPendingKey(scene), String(Date.now()));
    } catch (e) {
      setStatus("error");
    }
    timer.current = setTimeout(() => { void persist(v, state); }, 500);
  };
  const statusText = {
    loading: "读取服务器…",
    saving: "保存中…",
    saved: "已存服务器",
    local: "未同步（已留本机）",
    conflict: "与其他设备冲突",
    error: "保存失败，请复制留底",
  }[status] || "保存状态未知";
  const healthy = status === "saved";
  return (
    <>
      <div className="wr-notes-bar">
        <span className="wr-notes-cap">本场笔记</span>
        <span className={`wr-notes-state ${healthy ? "" : "saving"}`}>
          <span className="wr-notes-dot" />{statusText}
          {(status === "local" || status === "conflict" || status === "error") && (
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => { clearTimeout(timer.current); void persist(valueRef.current, sceneState.current); }}>重试</button>
          )}
        </span>
      </div>
      <textarea className="wr-notes" value={val} onChange={onChange} placeholder="只跟这一场有关的提醒、伏笔、待回收……（自动保存到服务器，按场独立）" />
    </>
  );
}
