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
   docs.open(id, onChange, { renamedFrom }) → { id, value(), status(), edit(value), retry(), close() }
     renamedFrom：这一份是另一个 id 换了名字（乐观新建的场：临时 sid → 稳定的 scene_id）——先等旧名字那一份离开时没存完的
     保存落地，再把它的本机缓存和未同步标记搬到新名字下（新名字下已有自己的一份就不盖），然后才读服务器；搬好之前先给旧名字的那一份。
   docs.rename(fromId, toId) → 同一件事，给不在打开着的那一份用（目录重读、认出换了名字的那一刻）；旧名字那一份正开着时
     返回 false、什么也不动（它关上、新名字那一份带着 renamedFrom 打开时会搬）。
   useRevisionedDoc(docs, id, { renamedFrom }) → { value, status, edit, retry }（组件用；换 id 时关上一份、开下一份）
   纯 ESM，不写 window。本场笔记（ws-writer-notes.jsx）用它。
   ========================================================== */

const { useEffect, useState } = React;

function readItem(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}

export function createRevisionedDocs({ cacheKey, pendingKey, load, save, reloadRevision, isConflict, debounceMs = 500 }) {
  const leavingChains = new Map();   // 缓存键 → 离开那一份时还没存完的保存链
  const openKeys = new Map();        // 缓存键 → 正开着的份数

  /* 旧名字下的本机缓存与未同步标记搬到新名字下。新名字下已有自己的一份：不盖它——旧名字下只是读缓存（没有未同步标记）
     就扔掉，标着未同步的原样留着（那是还没存上服务器的字）。 */
  function moveKeys(fromId, toId) {
    const from = { cache: cacheKey(fromId), pending: pendingKey(fromId) };
    const to = { cache: cacheKey(toId), pending: pendingKey(toId) };
    const cache = readItem(from.cache);
    const pending = readItem(from.pending);
    if (cache == null && pending == null) return;
    try {
      if (readItem(to.cache) == null && readItem(to.pending) == null) {
        if (cache != null) localStorage.setItem(to.cache, cache);
        if (pending != null) localStorage.setItem(to.pending, pending);
        localStorage.removeItem(from.cache);
        localStorage.removeItem(from.pending);
      } else if (pending == null) {
        localStorage.removeItem(from.cache);
      }
    } catch (e) { /* 存储被禁 / 满了：原样留着 */ }
  }

  function rename(fromId, toId) {
    if (!fromId || !toId || fromId === toId) return false;
    const fromKey = cacheKey(fromId);
    if (openKeys.get(fromKey)) return false;
    const leaving = leavingChains.get(fromKey);
    if (leaving) void leaving.then(() => moveKeys(fromId, toId));
    else moveKeys(fromId, toId);
    return true;
  }

  function open(id, onChange, { renamedFrom = null } = {}) {
    const key = cacheKey(id);
    const fromKey = renamedFrom && renamedFrom !== id ? cacheKey(renamedFrom) : null;
    /* 换了名字、新名字下还什么都没有：搬好之前先给旧名字的那一份（不闪一下空白） */
    const own = readItem(key);
    const stored = own != null || !fromKey ? own : readItem(fromKey);
    const pendingNow = readItem(pendingKey(id)) != null || (fromKey && own == null && readItem(pendingKey(renamedFrom)) != null);
    openKeys.set(key, (openKeys.get(key) || 0) + 1);
    const doc = {
      active: true,
      revision: 0,
      saveVersion: 0,
      editVersion: 0,
      unsaved: null,   // 防抖还没到点的那一稿（离开时立刻存）
      chain: Promise.resolve(),
      timer: null,
      value: stored != null ? stored : "",
      status: pendingNow ? "local" : "loading",
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
        const leaving = leavingChains.get(key) || (fromKey && leavingChains.get(fromKey));
        if (leaving) await leaving;
        if (!doc.active) return;
        if (fromKey) moveKeys(renamedFrom, id);
        const remote = await load(id);
        if (!remote || !doc.active) return;
        const remoteRevision = Number(remote.revision);
        if (Number.isFinite(remoteRevision)) doc.revision = Math.max(doc.revision, remoteRevision);
        if (doc.editVersion !== loadEditVersion) return;
        const serverValue = String(remote.value || "");
        const currentStored = readItem(key);
        if (readItem(pendingKey(id)) != null && currentStored != null && currentStored !== serverValue) {
          // 留着本机这一份（未同步，给「重试」）：显示的就是它（换了名字时它是刚从旧名字下搬过来的）
          update({ value: currentStored, status: "local" });
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
        const opened = (openKeys.get(key) || 1) - 1;
        if (opened > 0) openKeys.set(key, opened); else openKeys.delete(key);
        clearTimeout(doc.timer);
        if (doc.unsaved != null) void persist(doc.unsaved);
        const chain = doc.chain;
        leavingChains.set(key, chain);
        void chain.then(() => { if (leavingChains.get(key) === chain) leavingChains.delete(key); });
      },
    };
  }

  return { open, rename };
}

/* 组件里用一份：id 变了就关上一份（没到点的改动立刻存）、开下一份。
   renamedFrom：这一次换 id 其实是同一份换了名字（调用方判断），新的一份打开时把旧名字下的本机这一层搬过来 */
export function useRevisionedDoc(docs, id, { renamedFrom = null } = {}) {
  const [handle, setHandle] = useState(null);
  const [, rerender] = useState(0);
  useEffect(() => {
    const opened = docs.open(id, () => rerender((n) => n + 1), { renamedFrom });
    setHandle(opened);
    return () => { opened.close(); };
  }, [docs, id]); // eslint-disable-line react-hooks/exhaustive-deps
  const current = handle && handle.id === id ? handle : null;
  return {
    value: current ? current.value() : "",
    status: current ? current.status() : "loading",
    edit: (value) => { if (current) current.edit(value); },
    retry: () => { if (current) current.retry(); },
  };
}
