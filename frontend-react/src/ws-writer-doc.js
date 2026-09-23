import React from "react";
import { storeAlert } from "./lib/store-utils.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WrDocs } from "./wr-doc-store.jsx";
import { contentSafetyReviewFromError } from "./wr-content-safety-review.jsx";
import { copyGatePromoteMessage, finalGateNotes, isCopyGateError } from "./ws-copy-gate.js";
import { wsConfirm } from "./ws-notify.jsx";
import { WR_EMPTY_DOC, wrCountText, wrPrepareLoadedHTML, wrSerializeManuscript } from "./ws-writer-manuscript.js";
import { wrSceneIsApproved } from "./ws-writer-catalog.js";
import { useWrEvent } from "./ws-writer-hooks.js";

/* ==========================================================
   写作台正文与权威正文（2026-09-21 从 WriterRoom 拆出）
   ----------------------------------------------------------
   · useDocBinding：一场一份正文。换场时同步读 WrDocs 缓存进编辑器、后台水合服务端草稿；
     敲字后 900ms 自动保存（落盘的只有 wrSerializeManuscript 出来的干净正文）并把字数增量
     回写目录；离开这一场 / 卸载前把没落盘的改动用「当时的」场景 id 冲掉。
     decorate(el)：每次整段换掉编辑器内容之后调用（标实体、标批注）；
     afterLoad(el)：换场载入之后调用，可返回清理函数；beforeSave(el, sceneId)：落盘前调用。
   · useCanonicalPromotion：把已保存的草稿提升为权威正文，含内容风险逐项复核那一轮。
   ESM 模块，不写 window。
   ========================================================== */

const { useCallback, useEffect, useLayoutEffect, useRef, useState } = React;

export function useDocBinding({ activeScene, editorRef, counter, decorate, afterLoad, beforeSave }) {
  const [saved, setSaved] = useState("草稿已保存");
  const [savedAt, setSavedAt] = useState(null);
  const [canonicalStatus, setCanonicalStatus] = useState("unknown");
  const saveTimer = useRef(null);
  const baselineRef = useRef(0);   // 场景载入时的字数基线，增量回写用
  const dirtyRef = useRef(false);  // 有未落盘的改动
  const editVersionRef = useRef(0);
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

  const canonicalFromStore = (sceneId) => {
    const state = WrDocs.state(sceneId);
    return state && state.canonicalDirty === false ? "current" : "dirty";
  };

  /* 真·自动保存：正文落盘 + 字数增量回写目录/作品 */
  const persistDoc = useCallback(async () => {
    const el = editorRef.current;
    if (!el || !activeScene) return false;
    if (wrSceneIsApproved(activeScene)) {
      dirtyRef.current = false;
      setSaved("终稿已锁定");
      return false;
    }
    const sceneId = activeScene;
    beforeSaveEvent(el, sceneId);
    /* 落盘的只有干净正文：实体高亮、批注划线、改写标记、深改诊断、段落状态 class 都不进草稿 */
    const html = wrSerializeManuscript(el);
    const count = wrCountText(el);
    const editVersion = editVersionRef.current;
    const baseline = baselineRef.current;
    const stillCurrent = () => mountedRef.current && sceneRef.current === sceneId;
    try {
      await WrDocs.save(sceneId, html);
      if (WsCatalog) { try { WsCatalog.recordSceneWords(sceneId, count, baseline); } catch (e) {} }
      if (stillCurrent()) baselineRef.current = count;
      if (stillCurrent() && editVersionRef.current === editVersion) {
        dirtyRef.current = false;
        setSaved("草稿已保存");
        setSavedAt(Date.now());
        setCanonicalStatus(canonicalFromStore(sceneId));
      }
      return true;
    } catch (e) {
      if (stillCurrent() && editVersionRef.current === editVersion) {
        dirtyRef.current = true;
        setSaved("草稿保存失败");
      }
      return false;
    }
  }, [activeScene, editorRef, beforeSaveEvent]);

  const schedulePersist = useCallback(() => {
    if (wrSceneIsApproved(activeScene)) return;
    setSaved("正在保存草稿…");
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
    editVersionRef.current += 1;
    dirtyRef.current = false;
    setSaved("草稿已加载");
    setCanonicalStatus(canonicalFromStore(activeScene));
    let stored = null;
    try { stored = WrDocs.load(activeScene); } catch (e) {}
    el.innerHTML = wrPrepareLoadedHTML(stored) || WR_EMPTY_DOC;
    decorateEvent(el);
    baselineRef.current = wrCountText(el);
    recount();
    const cleanupAfterLoad = afterLoadEvent(el);

    /* 服务端草稿水合完成且本地无未保存改动 → 回填编辑器（跨浏览器以服务端为准） */
    const onDocLoaded = (e) => {
      if (!e || e.detail !== activeScene || dirtyRef.current) return;
      let fresh = null;
      try { fresh = WrDocs.load(activeScene); } catch (e2) {}
      const next = fresh == null ? null : wrPrepareLoadedHTML(fresh);
      if (next != null && wrSerializeManuscript(el) !== next) {
        el.innerHTML = next || WR_EMPTY_DOC;
        decorateEvent(el);
        baselineRef.current = wrCountText(el);
        recount();
      }
      setSaved("草稿已保存");
      setCanonicalStatus(canonicalFromStore(activeScene));
    };
    const onDocState = (e) => {
      if (!e || !e.detail || e.detail.sid !== activeScene) return;
      if (e.detail.lastSaveError) setSaved("草稿保存失败");
      else if (!e.detail.dirty) setSaved("草稿已保存");
      setCanonicalStatus(e.detail.canonicalDirty === false ? "current" : "dirty");
    };
    window.addEventListener("ws:wr-doc-loaded", onDocLoaded);
    window.addEventListener("ws:wr-doc-state", onDocState);
    /* 离开这个场景（或卸载）时，把未落盘的改动用「当时的」场景 id 冲掉 */
    const sid = activeScene;
    return () => {
      window.removeEventListener("ws:wr-doc-loaded", onDocLoaded);
      window.removeEventListener("ws:wr-doc-state", onDocState);
      if (typeof cleanupAfterLoad === "function") cleanupAfterLoad();
      clearTimeout(saveTimer.current);
      if (dirtyRef.current && sid && !wrSceneIsApproved(sid)) {
        try { beforeSaveEvent(el, sid); } catch (e) {}
        try { void WrDocs.save(sid, wrSerializeManuscript(el)).catch(() => {}); } catch (e) {}
        try { if (WsCatalog) WsCatalog.recordSceneWords(sid, wrCountText(el), baselineRef.current); } catch (e) {}
        dirtyRef.current = false;
      }
    };
  }, [activeScene]); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    saved, setSaved, savedAt, canonicalStatus, setCanonicalStatus,
    dirtyRef, recount, persistDoc, schedulePersist, cancelPendingSave,
  };
}

/* ---------------- 权威正文 ---------------- */

export function canonicalPromotionErrorMessage(error) {
  const code = error && error.code;
  if (code === "CANONICAL_BASE_CONFLICT" || code === "AUTHOR_DRAFT_CONFLICT") {
    return "草稿或权威正文已在别处更新。请刷新、比较最新版本后再提升。";
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
  const { setSaved, setCanonicalStatus, dirtyRef, persistDoc, cancelPendingSave } = doc;

  const promote = useWrEvent(async () => {
    if (!activeScene) return;
    if (wrSceneIsApproved(activeScene)) {
      storeAlert(null, "本场属于已批准终稿。请先到成稿中心重新打开章节，再修改正文。");
      return;
    }
    cancelPendingSave();
    if (dirtyRef.current) {
      setSaved("正在保存草稿…");
      const savedOk = await persistDoc();
      if (!savedOk) {
        storeAlert(null, "草稿还没有保存到服务端，暂时不能提升为权威正文。先让草稿保存成功再试。");
        return;
      }
    }
    /* 两个明确的选项代替浏览器的「确定 / 取消」：作者回答的是「改了什么」，不是「要不要继续」 */
    const confirmed = await wsConfirm({
      title: "这次只改了文字表达吗？",
      body: "提升为权威正文前请确认：人物、时间、地点、物品和剧情结果都没有变。改了故事事实的话先不要提升——系统不会静默沿用旧的叙事事件。",
      confirmLabel: "只改了文字，提升",
      cancelLabel: "改了事实，先不提升",
    });
    if (!confirmed) return;

    setCanonicalStatus("promoting");
    try {
      const data = await WrDocs.promote(activeScene, { narrativeEffect: "facts_unchanged" });
      setCanonicalStatus("current");
      notify(promotedMessage("草稿已提升为权威正文，场景记忆与章节汇总已随之重建。", data));
    } catch (e) {
      const code = e && e.code;
      if (code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
        const next = contentSafetyReviewFromError(e);
        if (next) {
          setReview({ ...next, sid: activeScene, acceptedCodes: [] });
          setError("");
          setCanonicalStatus("review");
          return;
        }
      }
      setCanonicalStatus(code === "CANONICAL_NARRATIVE_RECONCILIATION_REQUIRED" ? "reconcile" : "error");
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
    const received = Array.isArray(acceptedCodes) ? acceptedCodes : [];
    if (received.length !== exactCodes.length || exactCodes.some((code, index) => received[index] !== code)) {
      setError("确认项与服务端返回的风险代码不一致，已停止提升。");
      return;
    }
    setBusy(true);
    setError("");
    setCanonicalStatus("promoting");
    const acceptedForRetry = [...new Set([...(review.acceptedCodes || []), ...exactCodes])];
    try {
      const data = await WrDocs.promote(review.sid, {
        narrativeEffect: "facts_unchanged",
        acceptedWarningCodes: acceptedForRetry,
      });
      setReview(null);
      setCanonicalStatus("current");
      notify(promotedMessage("已按你的逐项确认重新校验，草稿已提升为权威正文。", data));
    } catch (e) {
      if (e && e.code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
        const next = contentSafetyReviewFromError(e);
        if (next) {
          setReview({ ...next, sid: review.sid, acceptedCodes: acceptedForRetry });
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
        setCanonicalStatus(e && e.code === "CANONICAL_NARRATIVE_RECONCILIATION_REQUIRED" ? "reconcile" : "error");
        storeAlert(null, canonicalPromotionErrorMessage(e));
      }
    } finally {
      setBusy(false);
    }
  });

  return { promote, review, busy, error, cancel, confirm };
}
