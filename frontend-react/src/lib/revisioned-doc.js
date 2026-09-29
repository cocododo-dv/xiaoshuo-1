import React from "react";

/* ==========================================================
   带修订号的写穿文档（2026-09-30，审计 F03-21）
   ----------------------------------------------------------
   一份按 id 存取的文本：本机留一份读缓存（cacheKey(id)）和「还没同步」标记（pendingKey(id)），
   改动防抖后带着修订号写到服务器；服务器回冲突（isConflict）时重读修订号、状态报冲突，由作者决定。
   · 同一份文档的保存串成一条链：后一次带着前一次拿回的修订号，不会乱序覆盖。
   · 离开（close）时没到点的改动立刻存、排队中的照常存完（都存回这一份，不会落到接着打开的那一份上）；
     下次打开同一份先等那条链落地再读服务器——否则会把自己刚发出去的那一次当成「别的设备改过」。
   · 读服务器晚到时，期间作者已经改过就不拿服务器的版本盖本机稿；上个会话留下的未同步稿
     （有标记、且与服务器不同）留着给「重试」，不冒充冲突——只有服务器真回冲突才说冲突。
   状态（status）：loading / saving / saved / local（未同步，已留本机）/ conflict / error。

   createRevisionedDocs({ cacheKey, pendingKey, load, save, reloadRevision, isConflict, debounceMs })
     load(id)                      → { value, revision } | null（还读不了：状态不变）
     save(id, value, baseRevision) → { revision }（冲突时抛 isConflict 认得的错误）
     reloadRevision(id)            → 服务器此刻的修订号（冲突之后用）
   docs.open(id, onChange) → { id, value(), status(), edit(value), retry(), close() }
   useRevisionedDoc(docs, id) → { value, status, edit, retry }（组件用；换 id 时关上一份、开下一份）
   纯 ESM，不写 window。本场笔记（ws-writer-notes.jsx）用它。
   ========================================================== */

const { useEffect, useState } = React;

function readItem(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}

export function createRevisionedDocs({ cacheKey, pendingKey, load, save, reloadRevision, isConflict, debounceMs = 500 }) {
  const leavingChains = new Map();   // 缓存键 → 离开那一份时还没存完的保存链

  function open(id, onChange) {
    const key = cacheKey(id);
    const stored = readItem(key);
    const doc = {
      active: true,
      revision: 0,
      saveVersion: 0,
      editVersion: 0,
      unsaved: null,   // 防抖还没到点的那一稿（离开时立刻存）
      chain: Promise.resolve(),
      timer: null,
      value: stored != null ? stored : "",
      status: readItem(pendingKey(id)) != null ? "local" : "loading",
    };
    /* 状态只在这一份还开着时报出去；关掉之后排队的保存照样存回这一份，只是不再改界面 */
    const update = (patch) => {
      Object.assign(doc, patch);
      if (doc.active && onChange) onChange();
    };

    const persist = (value) => {
      doc.unsaved = null;
      const version = ++doc.saveVersion;
      const persistedEditVersion = doc.editVersion;
      if (doc.active) update({ status: "saving" });
      doc.chain = doc.chain.then(async () => {
        try {
          const result = await save(id, value, doc.revision);
          const remoteRevision = Number(result && result.revision);
          doc.revision = Number.isFinite(remoteRevision) ? Math.max(doc.revision, remoteRevision) : doc.revision + 1;
          const latest = version === doc.saveVersion && persistedEditVersion === doc.editVersion;
          if (latest) {
            try { localStorage.removeItem(pendingKey(id)); } catch (e) {}
            if (doc.active) update({ status: "saved" });
          }
        } catch (error) {
          if (isConflict(error)) {
            try {
              const remoteRevision = Number(await reloadRevision(id));
              if (Number.isFinite(remoteRevision)) doc.revision = Math.max(doc.revision, remoteRevision);
            } catch (e) {}
            if (doc.active && version === doc.saveVersion) update({ status: "conflict" });
          } else if (doc.active && version === doc.saveVersion) {
            update({ status: "local" });
          }
        }
      });
      return doc.chain;
    };

    const loadEditVersion = doc.editVersion;
    void (async () => {
      try {
        const leaving = leavingChains.get(key);
        if (leaving) await leaving;
        if (!doc.active) return;
        const remote = await load(id);
        if (!remote || !doc.active) return;
        const remoteRevision = Number(remote.revision);
        if (Number.isFinite(remoteRevision)) doc.revision = Math.max(doc.revision, remoteRevision);
        if (doc.editVersion !== loadEditVersion) return;
        const serverValue = String(remote.value || "");
        const currentStored = readItem(key);
        if (readItem(pendingKey(id)) != null && currentStored != null && currentStored !== serverValue) {
          update({ status: "local" });
          return;
        }
        try {
          localStorage.setItem(key, serverValue);
          localStorage.removeItem(pendingKey(id));
        } catch (e) {}
        update({ value: serverValue, status: "saved" });
      } catch (e) {
        if (doc.active && doc.editVersion === loadEditVersion) update({ status: stored != null ? "local" : "error" });
      }
    })();

    return {
      id,
      value: () => doc.value,
      status: () => doc.status,
      edit(value) {
        if (!doc.active) return;
        doc.editVersion += 1;
        doc.unsaved = value;
        clearTimeout(doc.timer);
        let status = "saving";
        try {
          localStorage.setItem(key, value);
          localStorage.setItem(pendingKey(id), String(Date.now()));
        } catch (e) {
          status = "error";
        }
        update({ value, status });
        doc.timer = setTimeout(() => { void persist(value); }, debounceMs);
      },
      retry() {
        if (!doc.active) return;
        clearTimeout(doc.timer);
        void persist(doc.value);
      },
      close() {
        if (!doc.active) return;
        doc.active = false;
        clearTimeout(doc.timer);
        if (doc.unsaved != null) void persist(doc.unsaved);
        const chain = doc.chain;
        leavingChains.set(key, chain);
        void chain.then(() => { if (leavingChains.get(key) === chain) leavingChains.delete(key); });
      },
    };
  }

  return { open };
}

/* 组件里用一份：id 变了就关上一份（没到点的改动立刻存）、开下一份 */
export function useRevisionedDoc(docs, id) {
  const [handle, setHandle] = useState(null);
  const [, rerender] = useState(0);
  useEffect(() => {
    const opened = docs.open(id, () => rerender((n) => n + 1));
    setHandle(opened);
    return () => { opened.close(); };
  }, [docs, id]);
  const current = handle && handle.id === id ? handle : null;
  return {
    value: current ? current.value() : "",
    status: current ? current.status() : "loading",
    edit: (value) => { if (current) current.edit(value); },
    retry: () => { if (current) current.retry(); },
  };
}
