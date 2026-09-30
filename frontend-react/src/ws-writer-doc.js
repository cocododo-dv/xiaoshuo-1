import React from "react";
import { storeAlert } from "./lib/store-utils.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WrDocs } from "./wr-doc-store.jsx";
import { sameManuscriptText } from "./wr-doc-cache.js";
import { contentSafetyReviewFromError, exactCodesMatch } from "./wr-content-safety-review.jsx";
import { copyGatePromoteMessage, finalGateNotes, isCopyGateError } from "./ws-copy-gate.js";
import { wsConfirm } from "./ws-notify.jsx";
import { WR_EMPTY_DOC, wrCountText, wrPrepareLoadedHTML, wrSerializeManuscript } from "./ws-writer-manuscript.js";
import { wrSceneIsApproved } from "./ws-writer-catalog.js";
import { useWrEvent } from "./ws-writer-hooks.js";

/* ==========================================================
   写作台正文与权威正文（2026-09-21 从 WriterRoom 拆出；2026-09-30 W1 保存全交给 WrDocs）
   ----------------------------------------------------------
   · useDocBinding：一场一份正文。换场时同步读 WrDocs 缓存进编辑器、后台水合服务端草稿；
     敲字后 900ms 自动保存（落盘的只有 wrSerializeManuscript 出来的干净正文；目录字数随保存
     回包的 words_rollup 更新）；离开这一场 / 卸载前、页面被刷新 / 关掉 / 切到后台时（pagehide、
     visibilitychange），把没交出去的字用「当时的」场景 id 和作品交给 WrDocs。
     保存的一切——同一场一次只有一个请求、排队只留最新一稿、失败留在本机待重发、409 的冲突副本和
     读服务端版本——都在 WrDocs（wr-doc-sync.js）里，这里不排队、不重试、不自己处理冲突：
     自动保存 / 离场 / 提升 / 进深改前只管 WrDocs.save + WrDocs.flush，然后听 WrDocs 的通知——
     conflict-resolved：编辑器换成服务端版本（编辑器里还没交出去的字先经 WrDocs 留进同步与恢复）；
     loaded：读缓存换成了别的版本（水合 / 复核读到的服务端新版本、恢复、采纳）：作者正在旧版本上写，
     同样先留一份再换；读到的就是作者正在写的底稿，就接着写（force——服务端拒绝了本机的字、章已锁定——时也换）；
     换上的正文还在等保存（恢复稿）时状态照 WrDocs 说；
     state：冲突中（服务端版本还没读到）或保存失败时状态是「草稿保存失败」，编辑器里的字不动；冲突中接着敲字
     也不闪「正在保存」（WrDocs 不会发）。
     章已批准锁定的场也照常把没交出去的字交给 WrDocs：WrDocs 不替它保存，那几句留进同步与恢复、编辑器换回终稿正文
     （复核三 W1-R3B-3：写作台转只读那一刻还没到自动保存的半句，过去在这里被丢掉）。
     decorate(el)：每次整段换掉编辑器内容之后调用（标实体、标批注）；
     afterLoad(el)：换场载入之后调用，可返回清理函数；beforeSave(el, sceneId)：交给 WrDocs 之前调用。
   · useCanonicalPromotion：把已保存的草稿提升为权威正文，含内容风险逐项复核那一轮。
   ESM 模块，不写 window。
   ========================================================== */

const { useCallback, useEffect, useLayoutEffect, useRef, useState } = React;

/* 当前作品 id（离场时这一场所属的作品）。try 是有用的：不少单测 mock 的 WsWorks 没有 activeId */
function currentWorkId() {
  try { return WsWorks.activeId() || ""; } catch (e) { return ""; }
}

/* 这一场所属的作品已经不是当前作品（离场冲刷、切作品那一刻到点的自动保存）时才把作品带给 WrDocs；平常照常按当前作品 */
function workScope(workId) {
  return workId && workId !== currentWorkId() ? { workId } : null;
}
function saveToWork(sid, html, workId) {
  const scope = workScope(workId);
  // 结果由 WrDocs 管（失败留在本机待重发，409 走冲突），这里不接；Promise.resolve 兜住单测里不回 Promise 的替身
  try {
    void Promise.resolve(scope ? WrDocs.save(sid, html, scope) : WrDocs.save(sid, html)).catch(() => {});
  } catch (e) { /* 同上 */ }
}

/* WrDocs 的状态快照 → 保存状态键（loaded / saving / saved / failed / locked，字在 wr-canonical-control 的 SAVE_LABELS）。
   idle：没有要保存的字时说什么（刚换场是「已加载」，读缓存换成新版本后是「已保存」或「已加载」） */
function saveStatusOf(state, idle = "loaded") {
  if (!state) return idle;
  if (state.conflictPending || state.lastSaveError) return "failed";
  return state.dirty ? "saving" : idle;
}

/* WrDocs 眼下的权威正文状态（提升回来时草稿可能已经又往前走了一版） */
function canonicalStatusOf(sceneId) {
  const state = WrDocs.state(sceneId);
  return state && state.canonicalDirty === false ? "current" : "dirty";
}

export function useDocBinding({ activeScene, editorRef, counter, decorate, afterLoad, beforeSave }) {
  const [saved, setSaved] = useState("saved");
  const [savedAt, setSavedAt] = useState(null);
  const [canonicalStatus, setCanonicalStatus] = useState("unknown");
  const saveTimer = useRef(null);
  const dirtyRef = useRef(false);     // 有还没确认存上的字：敲字即为 true，WrDocs 说存上了（或编辑器整篇换稿）才清
  const editVersionRef = useRef(0);   // 每敲一次、每次整篇换稿都 +1
  const handedRef = useRef(0);        // 交给 WrDocs 的最新一版；和 editVersionRef 相等 = 编辑器里的字 WrDocs 都有了
  const baseRef = useRef("");         // 编辑器上一次整篇换稿后的正文：作者是在它上面写的
  const workRef = useRef("");         // 这一场所属的作品：保存 / 冲刷都带上它（离场时作品可能已经换了）
  const mountedRef = useRef(false);
  const sceneRef = useRef(activeScene);
  const decorateEvent = useWrEvent((el) => { if (decorate) decorate(el); });
  const afterLoadEvent = useWrEvent((el) => (afterLoad ? afterLoad(el) : null));
  const beforeSaveEvent = useWrEvent((el, sceneId) => { if (beforeSave) beforeSave(el, sceneId); });

  // 保存完成时拿它判断「还是不是这一场」：换场一提交就更新，不等副作用
  useLayoutEffect(() => { sceneRef.current = activeScene; }, [activeScene]);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; clearTimeout(saveTimer.current); };
  }, []);

  /* 字数进小仓库（只有读字数的叶子组件重渲染）；空白页的提示由 CSS 按 data-empty 画 */
  const recount = useCallback(() => {
    const el = editorRef.current;
    if (!el) return;
    const count = wrCountText(el);
    counter.set(count);
    el.setAttribute("data-empty", count === 0 ? "on" : "off");
  }, [counter, editorRef]);

  const canonicalFromStore = canonicalStatusOf;

  /* 编辑器里的字交给 WrDocs（有没交出去的才交）。之后的一切——本机缓存、排队、重发、冲突、章已锁定——WrDocs 管 */
  const handOver = (el, sceneId) => {
    if (editVersionRef.current === handedRef.current) return;
    beforeSaveEvent(el, sceneId);
    const html = wrSerializeManuscript(el);
    handedRef.current = editVersionRef.current;
    saveToWork(sceneId, html, workRef.current);
  };

  /* 把编辑器里的字交给 WrDocs 并等结果 → "saved" | "conflict" | "refused" | "failed" | "locked" | "dirty"（等的时候又敲了字）| "none" */
  const settle = async (options) => {
    const sceneId = activeScene;
    const el = editorRef.current;
    if (!el || !sceneId) return "none";
    // 章已批准锁定：还没交出去的字照样交——WrDocs 不替它保存，留进同步与恢复、编辑器换回终稿正文
    handOver(el, sceneId);
    if (wrSceneIsApproved(sceneId)) {
      dirtyRef.current = false;
      setSaved("locked");
      return "locked";
    }
    const editVersion = editVersionRef.current;
    const outcome = await WrDocs.flush(sceneId, { ...(options || {}), ...(workScope(workRef.current) || {}) });
    if (!mountedRef.current || sceneRef.current !== sceneId) return outcome;
    // 等的时候又敲了字，或编辑器已经整篇换成服务端版本（冲突）：下一次再定
    if (editVersionRef.current !== editVersion) return outcome === "saved" ? "dirty" : outcome;
    if (outcome === "saved") {
      dirtyRef.current = false;
      setSaved("saved");
      setSavedAt(Date.now());
      setCanonicalStatus(canonicalFromStore(sceneId));
    } else if (outcome === "refused") {
      dirtyRef.current = false;
      setSaved("loaded"); // 服务端拒绝了：WrDocs 已换回服务端版本、告诉了作者
    } else {
      setSaved("failed"); // 冲突中 / 保存失败：编辑器里的字不动，dirty 不清
    }
    return outcome;
  };

  /* 真·自动保存：返回这之后编辑器里的字是否都已存上（深改进场前、提升前也走它） */
  const persistDoc = useCallback(async () => (await settle()) === "saved", [activeScene, editorRef, beforeSaveEvent]); // eslint-disable-line react-hooks/exhaustive-deps

  const schedulePersist = useCallback(() => {
    if (wrSceneIsApproved(activeScene)) return;
    // 冲突中（服务端版本还没读到）WrDocs 不会发这一稿：状态留在「草稿保存失败」，不闪「正在保存」
    const state = WrDocs.state(activeScene);
    setSaved(state && state.conflictPending ? "failed" : "saving");
    setCanonicalStatus("dirty");
    dirtyRef.current = true;
    editVersionRef.current += 1;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => { void persistDoc(); }, 900);
  }, [activeScene, persistDoc]);

  const cancelPendingSave = useCallback(() => { clearTimeout(saveTimer.current); }, []);

  /* 换场：同步读 WrDocs 缓存（兼容旧 wr-doc 本地键），后台水合服务端草稿。
     wrPrepareLoadedHTML 顺手拆掉历史遗留的空壳标记和旧开场占位句；空白场放一个空段落，
     提示语由 CSS 画在空段落上（不再是一段会被存下去的「正文」）。 */
  useEffect(() => {
    const el = editorRef.current;
    if (!el) return undefined;
    if (!activeScene) {
      el.innerHTML = "";
      recount();
      setCanonicalStatus("unknown");
      return undefined;
    }
    const sid = activeScene;
    const workId = currentWorkId();
    workRef.current = workId;
    editVersionRef.current += 1;
    handedRef.current = editVersionRef.current;
    const initial = WrDocs.state(sid);
    dirtyRef.current = !!(initial && initial.dirty); // WrDocs 这一场还有没存上的字（路上 / 排队 / 失败待重发 / 冲突中）
    setSaved(saveStatusOf(initial));
    setCanonicalStatus(canonicalFromStore(sid));
    let stored = null;
    try { stored = WrDocs.load(sid); } catch (e) {}
    el.innerHTML = wrPrepareLoadedHTML(stored) || WR_EMPTY_DOC;
    baseRef.current = wrSerializeManuscript(el);
    decorateEvent(el);
    recount();
    const cleanupAfterLoad = afterLoadEvent(el);

    /* 编辑器整篇换成 html（冲突之后的服务端版本 / 读缓存换成的新版本）。还没交给 WrDocs 的字是在上一个版本上写的：先经 WrDocs 留进同步与恢复 */
    const replaceEditor = (html, reason) => {
      if (editVersionRef.current !== handedRef.current) {
        try { WrDocs.keepLocalCopy(sid, wrSerializeManuscript(el), { reason, ...(workScope(workId) || {}) }); } catch (e) {}
      }
      clearTimeout(saveTimer.current);
      editVersionRef.current += 1;
      handedRef.current = editVersionRef.current;
      dirtyRef.current = false;
      const next = wrPrepareLoadedHTML(html == null ? "" : html);
      if (wrSerializeManuscript(el) !== next) {
        el.innerHTML = next || WR_EMPTY_DOC;
        decorateEvent(el);
        recount();
      }
      baseRef.current = wrSerializeManuscript(el);
      setCanonicalStatus(canonicalFromStore(sid));
    };
    /* 读缓存换成了别的版本：作者正在写的就是这份底稿（文字一样）时接着写，下一次保存带新的修订号；否则换稿。
       force（服务端拒绝了本机的字 / 章已锁定）：接着写也存不上——照样换，正在写的那几句先留。
       换上的正文还在等保存（恢复稿）或没存上时，状态照 WrDocs 的说 */
    const onLoaded = (detail) => {
      const typing = editVersionRef.current !== handedRef.current;
      if (!detail.force && typing && sameManuscriptText(detail.html, baseRef.current)) return;
      replaceEditor(detail.html, detail.reason || "server");
      setSaved(detail.force ? "loaded" : saveStatusOf(detail, typing ? "loaded" : "saved"));
    };
    const onResolved = (detail) => {
      replaceEditor(detail.html, detail.reason || "conflict");
      setSaved("loaded");
    };
    const onState = (detail) => {
      setCanonicalStatus(detail.canonicalDirty === false ? "current" : "dirty");
      if (detail.conflictPending || detail.lastSaveError) { setSaved("failed"); return; }
      if (!detail.dirty && editVersionRef.current === handedRef.current) {
        dirtyRef.current = false;
        setSaved("saved");
      }
    };
    const unsubscribe = WrDocs.subscribe((kind, detail) => {
      if (!detail || detail.sid !== sid || (detail.workId != null && detail.workId !== workId)) return;
      if (kind === "state") onState(detail);
      else if (kind === "loaded") onLoaded(detail);
      else if (kind === "conflict-resolved") onResolved(detail);
    });
    /* 编辑器里还没交出去的字，用「当时的」场景 id 和作品交给 WrDocs（它在调用之内就写进本机缓存和未同步标记；
       章已批准锁定的场，它在调用之内留进同步与恢复）。交了返回 true */
    const handOverNow = () => {
      if (editVersionRef.current === handedRef.current) return false;
      try { beforeSaveEvent(el, sid); } catch (e) {}
      handedRef.current = editVersionRef.current;
      saveToWork(sid, wrSerializeManuscript(el), workId);
      return true;
    };
    /* 刷新 / 关掉标签页 / 切到后台：React 的清理不会跑，900 ms 的自动保存也可能来不及——这几句先交出去，
       刷新之后按跨会话的路径还在（本机缓存 + 未同步标记） */
    const onPageHide = () => { handOverNow(); };
    const onVisibility = () => { if (document.visibilityState === "hidden") handOverNow(); };
    window.addEventListener("pagehide", onPageHide);
    document.addEventListener("visibilitychange", onVisibility);
    /* 离开这个场景（或卸载）时：没交出去的字交给 WrDocs；再让 WrDocs 冲刷一次——
       路上那一次（或刚交的这一稿）失败时它补发一次最新的一稿，409 由它走冲突副本 */
    return () => {
      unsubscribe();
      window.removeEventListener("pagehide", onPageHide);
      document.removeEventListener("visibilitychange", onVisibility);
      if (typeof cleanupAfterLoad === "function") cleanupAfterLoad();
      clearTimeout(saveTimer.current);
      const handed = handOverNow(); // 章已批准锁定的场也交：WrDocs 把那几句留进同步与恢复
      if (!wrSceneIsApproved(sid)) {
        // 不等回包的离场冲刷：先按本机计数更新目录，回包的 words_rollup 到了再以服务端为准
        if (handed && currentWorkId() === workId) {
          try { WsCatalog.recordSceneWords(sid, wrCountText(el)); } catch (e) {}
        }
        try { void WrDocs.flush(sid, { retry: true, ...(workScope(workId) || {}) }); } catch (e) {}
      }
      dirtyRef.current = false;
    };
  }, [activeScene]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 编辑器眼下的正文（提升前问作者「只改了文字吗」时，问的就是这一稿） */
  const shownHTML = useCallback(() => (editorRef.current ? wrSerializeManuscript(editorRef.current) : null), [editorRef]);

  return {
    saved, setSaved, savedAt, canonicalStatus, setCanonicalStatus,
    dirtyRef, recount, persistDoc, schedulePersist, cancelPendingSave, settle, shownHTML,
  };
}

/* ---------------- 权威正文 ---------------- */

export function canonicalPromotionErrorMessage(error) {
  const code = error && error.code;
  if (code === "CANONICAL_BASE_CONFLICT" || code === "AUTHOR_DRAFT_CONFLICT") {
    return "草稿或权威正文已在别处更新。请刷新、比较最新版本后再提升。";
  }
  // 提升途中作者接着写、自动保存先到了服务端：不是别处的改动（复核三 W1-R3B-8）
  if (code === "AUTHOR_DRAFT_MOVED_BY_SELF") {
    return "提升途中你又改了几句，已经保存；这次没有提升。看过之后再点一次「提升为权威正文」。";
  }
  if (code === "CANONICAL_NARRATIVE_RECONCILIATION_REQUIRED") {
    return "这次修改涉及故事事实，必须先核对叙事事件，系统不会静默沿用旧事实。";
  }
  if (code === "CHAPTER_APPROVED_LOCKED") {
    return "该章节已批准锁定，需要先明确重开章节，才能更新权威正文。";
  }
  if (code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
    return "服务端要求内容风险复核，但没有返回可逐项确认的项目。系统已停止提升，请刷新后重试。";
  }
  // 唯一抄袭门拦下（与参考书原文连续相同）：说是第几字、草稿没动；不把后端的英文原话甩给作者
  if (isCopyGateError(error)) return copyGatePromoteMessage(error);
  return "权威正文提升失败，草稿仍安全保留。请稍后重试。";
}

/* 提升成功的回执：成稿门的不拦警告（用了参考书的专名等）接在后面说一句 */
function promotedMessage(base, data) {
  const notes = finalGateNotes(data);
  return notes.length ? `${base}${notes.join("")}` : base;
}

/* doc：useDocBinding 的返回；notify(message)：成功回执 */
export function useCanonicalPromotion({ activeScene, doc, notify }) {
  const [review, setReview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { setSaved, setCanonicalStatus, dirtyRef, settle, cancelPendingSave, shownHTML } = doc;

  /* 提升没成之后权威正文的状态：作者自己接着写、草稿已往前走（MOVED_BY_SELF）是「待更新」，不是「提升失败」 */
  const failedCanonicalStatus = (sceneId, code) => {
    if (code === "CANONICAL_NARRATIVE_RECONCILIATION_REQUIRED") return "reconcile";
    return code === "AUTHOR_DRAFT_MOVED_BY_SELF" ? canonicalStatusOf(sceneId) : "error";
  };

  const promote = useWrEvent(async () => {
    if (!activeScene) return;
    if (wrSceneIsApproved(activeScene)) {
      storeAlert(null, "本场属于已批准终稿。请先到成稿中心重新打开章节，再修改正文。");
      return;
    }
    cancelPendingSave();
    const state = WrDocs.state(activeScene);
    if (dirtyRef.current || (state && (state.dirty || state.conflictPending))) {
      setSaved("saving");
      const outcome = await settle();
      if (outcome === "conflict") {
        // 409：编辑器已换成（或正要换成）服务端版本，本机的字在同步与恢复——说的是冲突，不是「还没保存」
        storeAlert(null, canonicalPromotionErrorMessage({ code: "AUTHOR_DRAFT_CONFLICT" }));
        return;
      }
      if (outcome === "refused") return; // 服务端拒绝了这一稿：WrDocs 已换回服务端版本并告诉了作者
      if (outcome !== "saved") {
        storeAlert(null, "草稿还没有保存到服务端，暂时不能提升为权威正文。先让草稿保存成功再试。");
        return;
      }
    }
    /* 两个明确的选项代替浏览器的「确定 / 取消」：作者回答的是「改了什么」，不是「要不要继续」。
       问的是编辑器眼下这一稿：记下它，确认框开着时水合 / 后台复核落地、编辑器被换成了别处的版本，WrDocs 就不提升
       （作者确认的不是那一稿；复核三 W1-R3A-1 · W1-R3B-5） */
    const asked = shownHTML();
    const confirmed = await wsConfirm({
      title: "这次只改了文字表达吗？",
      body: "提升为权威正文前请确认：人物、时间、地点、物品和剧情结果都没有变。改了故事事实的话先不要提升——系统不会静默沿用旧的叙事事件。",
      confirmLabel: "只改了文字，提升",
      cancelLabel: "改了事实，先不提升",
    });
    if (!confirmed) return;

    setCanonicalStatus("promoting");
    try {
      const data = await WrDocs.promote(activeScene, { narrativeEffect: "facts_unchanged", expectedText: asked });
      // 提升在路上时作者又存了一稿：提升的是较早的那个修订号，照 WrDocs 说「待更新」，不说「已更新」
      setCanonicalStatus(canonicalStatusOf(activeScene));
      notify(promotedMessage("草稿已提升为权威正文，场景记忆与章节汇总已随之重建。", data));
    } catch (e) {
      const code = e && e.code;
      if (code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
        const next = contentSafetyReviewFromError(e);
        if (next) {
          setReview({ ...next, sid: activeScene, acceptedCodes: [], expectedText: asked });
          setError("");
          setCanonicalStatus("review");
          return;
        }
      }
      setCanonicalStatus(failedCanonicalStatus(activeScene, code));
      storeAlert(null, canonicalPromotionErrorMessage(e));
    }
  });

  const cancel = useWrEvent(() => {
    if (busy) return;
    setReview(null);
    setError("");
    setCanonicalStatus("dirty");
  });

  const confirm = useWrEvent(async (acceptedCodes) => {
    if (!review || busy) return;
    const exactCodes = review.findings.map((item) => item.code);
    if (!exactCodesMatch(exactCodes, acceptedCodes)) {
      setError("确认项与服务端返回的风险代码不一致，已停止提升。");
      return;
    }
    setBusy(true);
    setError("");
    setCanonicalStatus("promoting");
    const acceptedForRetry = [...new Set([...(review.acceptedCodes || []), ...exactCodes])];
    try {
      // 逐项复核的还是作者当初确认「只改了文字」的那一稿：这期间编辑器被换成了别处的版本就不提升
      const data = await WrDocs.promote(review.sid, {
        narrativeEffect: "facts_unchanged",
        acceptedWarningCodes: acceptedForRetry,
        ...(review.expectedText !== undefined ? { expectedText: review.expectedText } : {}),
      });
      setReview(null);
      setCanonicalStatus(canonicalStatusOf(review.sid));
      notify(promotedMessage("已按你的逐项确认重新校验，草稿已提升为权威正文。", data));
    } catch (e) {
      if (e && e.code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
        const next = contentSafetyReviewFromError(e);
        if (next) {
          setReview({ ...next, sid: review.sid, acceptedCodes: acceptedForRetry, expectedText: review.expectedText });
          setError("服务端返回了仍需复核的项目。系统没有沿用上次勾选，请重新逐项核对。");
          setCanonicalStatus("review");
          return;
        }
      }
      if (e && (e.retryable || e.code === "NETWORK_ERROR" || Number(e.status) >= 500)) {
        setError(`${canonicalPromotionErrorMessage(e)} 你可以保持本页并重试。`);
        setCanonicalStatus("review");
      } else {
        setReview(null);
        setCanonicalStatus(failedCanonicalStatus(review.sid, e && e.code));
        storeAlert(null, canonicalPromotionErrorMessage(e));
      }
    } finally {
      setBusy(false);
    }
  });

  return { promote, review, busy, error, cancel, confirm };
}
