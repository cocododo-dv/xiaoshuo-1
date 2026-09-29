import React from "react";
import { emit } from "./lib/events.js";
import { storeAlert } from "./lib/store-utils.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WrDocs, WrRecovery } from "./wr-doc-store.jsx";
import { contentSafetyReviewFromError, exactCodesMatch } from "./wr-content-safety-review.jsx";
import { copyGatePromoteMessage, finalGateNotes, isCopyGateError } from "./ws-copy-gate.js";
import { wsConfirm, wsNotify } from "./ws-notify.jsx";
import { WR_EMPTY_DOC, wrCountText, wrPrepareLoadedHTML, wrSerializeManuscript } from "./ws-writer-manuscript.js";
import { wrSceneIsApproved } from "./ws-writer-catalog.js";
import { useWrEvent } from "./ws-writer-hooks.js";

/* ==========================================================
   写作台正文与权威正文（2026-09-21 从 WriterRoom 拆出）
   ----------------------------------------------------------
   · useDocBinding：一场一份正文。换场时同步读 WrDocs 缓存进编辑器、后台水合服务端草稿；
     敲字后 900ms 自动保存（落盘的只有 wrSerializeManuscript 出来的干净正文；目录字数随保存
     回包的 words_rollup 更新）；离开这一场 / 卸载前把没落盘的改动用「当时的」场景 id 冲掉。
     同一场同一时刻只有一次保存在路上：下一次等它（连同 409 之后 WrDocs 的重新水合）落地再定——
     叠上去的保存会带着刷新后的修订号，把另一台设备同时存下的正文静默盖掉。
     409 时编辑器一律换成服务端版本，本机的字（那一稿、它在路上时又敲的）都在「同步与恢复」。
     decorate(el)：每次整段换掉编辑器内容之后调用（标实体、标批注）；
     afterLoad(el)：换场载入之后调用，可返回清理函数；beforeSave(el, sceneId)：落盘前调用。
   · useCanonicalPromotion：把已保存的草稿提升为权威正文，含内容风险逐项复核那一轮。
   ESM 模块，不写 window。
   ========================================================== */

const { useCallback, useEffect, useLayoutEffect, useRef, useState } = React;

/* 当前作品 id（离场后补发那一稿之前核对：作品换了就不发，免得落到另一部作品同名的场上）。
   try 是有用的：不少单测 mock 的 WsWorks 没有 activeId */
function currentWorkId() {
  try { return WsWorks.activeId() || ""; } catch (e) { return ""; }
}

/* 409 时保存路上又敲的那几个字放不进本机存储（配额满了）：它只留在本次会话的「同步与恢复」里，
   刷新或关掉页面就没了——编辑器已经换成服务端版本，这里说清楚、给一个直接打开它的按钮。 */
function volatileConflictNotice(sceneId) {
  wsNotify({
    message: "这份正文在别处被修改过，编辑器已换成服务端的最新版本。保存进行中你又写的那几句因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。",
    tone: "danger",
    timeout: 20000,
    action: { label: "打开同步与恢复", onClick: () => { emit("ws:recovery-open", { sid: sceneId }); } },
  });
}

export function useDocBinding({ activeScene, editorRef, counter, decorate, afterLoad, beforeSave }) {
  // 保存状态的键（loaded / saving / saved / failed / locked），字在 wr-canonical-control 的 SAVE_LABELS
  const [saved, setSaved] = useState("saved");
  const [savedAt, setSavedAt] = useState(null);
  const [canonicalStatus, setCanonicalStatus] = useState("unknown");
  const saveTimer = useRef(null);
  const dirtyRef = useRef(false);  // 有未落盘的改动（保存在路上时仍为 true，存上了才清）
  const editVersionRef = useRef(0);
  /* 每一场在路上的那一次保存：sceneId → { editVersion, html, done }。done 在它（连同 409 之后换稿）
     处理完时兑现为 "saved" / "conflict" / "failed"——等着存下一稿的、离场冲刷都看它。 */
  const inFlightRef = useRef(new Map());
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

  /* 把读缓存里的这一场放进编辑器（服务端水合 / 409 冲突之后） */
  const showStored = (el, sceneId) => {
    let fresh = null;
    try { fresh = WrDocs.load(sceneId); } catch (e) {}
    const next = fresh == null ? null : wrPrepareLoadedHTML(fresh);
    if (next != null && wrSerializeManuscript(el) !== next) {
      el.innerHTML = next || WR_EMPTY_DOC;
      decorateEvent(el);
      recount();
    }
  };

  /* 409 冲突：WrDocs 已把本机那一稿放进「同步与恢复」、读缓存换成了服务端版本（toast 这么说）。
     编辑器必须跟着换，否则下一次敲字会带着新的 base_revision_no 把另一台设备的正文盖掉。
     保存在路上时作者又敲的字不在那份副本里，先另存一份再换；那一份放不进本机存储时照样换
     （WrDocs 的修订号已经刷新，留着本机稿只会在下一次敲字时静默盖掉服务端版本），并说清它只在本次会话里。
     WrDocs 自己没能留副本（配额不足）时它不换读缓存、也不刷新修订号（下一次保存照旧 409），编辑器也不动。 */
  const adoptServerAfterConflict = (el, sceneId, savedHTML, editVersion) => {
    const state = WrDocs.state(sceneId);
    if (!state || state.dirty) return false;
    let pendingBackup = null;
    if (editVersionRef.current !== editVersion) {
      const pendingHTML = wrSerializeManuscript(el);
      if (pendingHTML !== savedHTML) {
        pendingBackup = WrRecovery.create({
          sid: sceneId,
          html: pendingHTML,
          type: "conflict",
          reason: "服务端在别处更新（409 冲突）时编辑器里还有没保存的改动",
          label: `场景 ${sceneId} · 冲突本地稿`,
        });
      }
    }
    clearTimeout(saveTimer.current);
    editVersionRef.current += 1;
    dirtyRef.current = false;
    showStored(el, sceneId);
    setSaved("loaded");
    setCanonicalStatus(canonicalFromStore(sceneId));
    if (pendingBackup && pendingBackup.durable === false) volatileConflictNotice(sceneId);
    return true;
  };

  /* 真·自动保存：正文落盘 + 字数增量回写目录/作品。返回这之后编辑器里的字是否都已存上。 */
  const persistDoc = useCallback(async () => {
    const sceneId = activeScene;
    if (!editorRef.current || !sceneId) return false;
    const stillCurrent = () => mountedRef.current && sceneRef.current === sceneId;
    /* 上一次保存还在路上：等它（连同 409 之后的换稿）落地再定。它撞上 409 时这一次作废——
       编辑器要么已换成服务端版本（这段字已在同步与恢复），要么 WrDocs 拒绝覆盖、等作者处理，都不自动再存。 */
    let prior = null;
    while (inFlightRef.current.has(sceneId)) {
      prior = await inFlightRef.current.get(sceneId).done;
      if (!stillCurrent()) return false;
    }
    if (prior === "conflict") return false;
    const el = editorRef.current;
    if (!el) return false;
    if (wrSceneIsApproved(sceneId)) {
      dirtyRef.current = false;
      setSaved("locked");
      return false;
    }
    if (!dirtyRef.current) return prior !== "failed";   // 在路上那一次已经把这些字存上了
    beforeSaveEvent(el, sceneId);
    /* 落盘的只有干净正文：实体高亮、批注划线、改写标记、深改诊断、段落状态 class 都不进草稿 */
    const html = wrSerializeManuscript(el);
    const editVersion = editVersionRef.current;
    let settle = null;
    const run = { editVersion, html, done: new Promise((resolve) => { settle = resolve; }) };
    inFlightRef.current.set(sceneId, run);
    let outcome = "failed";
    try {
      // 目录字数以保存回包的 words_rollup 为准（WrDocs 已经写进目录），这里不再拿本机计数盖它
      await WrDocs.save(sceneId, html);
      outcome = "saved";
      if (stillCurrent() && editVersionRef.current === editVersion) {
        dirtyRef.current = false;
        setSaved("saved");
        setSavedAt(Date.now());
        setCanonicalStatus(canonicalFromStore(sceneId));
      }
      return true;
    } catch (e) {
      if (e && e.code === "AUTHOR_DRAFT_CONFLICT") {
        outcome = "conflict";
        if (stillCurrent() && adoptServerAfterConflict(el, sceneId, html, editVersion)) return false;
      }
      if (stillCurrent() && editVersionRef.current === editVersion) {
        dirtyRef.current = true;
        setSaved("failed");
      }
      return false;
    } finally {
      if (inFlightRef.current.get(sceneId) === run) inFlightRef.current.delete(sceneId);
      settle(outcome);
    }
  }, [activeScene, editorRef, beforeSaveEvent]);

  const schedulePersist = useCallback(() => {
    if (wrSceneIsApproved(activeScene)) return;
    setSaved("saving");
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
    setSaved("loaded");
    setCanonicalStatus(canonicalFromStore(activeScene));
    let stored = null;
    try { stored = WrDocs.load(activeScene); } catch (e) {}
    el.innerHTML = wrPrepareLoadedHTML(stored) || WR_EMPTY_DOC;
    decorateEvent(el);
    recount();
    const cleanupAfterLoad = afterLoadEvent(el);

    /* 服务端草稿水合完成且本地无未保存改动 → 回填编辑器（跨浏览器以服务端为准） */
    const onDocLoaded = (sid) => {
      if (sid !== activeScene || dirtyRef.current) return;
      showStored(el, activeScene);
      setSaved("saved");
      setCanonicalStatus(canonicalFromStore(activeScene));
    };
    const onDocState = (detail) => {
      if (!detail || detail.sid !== activeScene) return;
      if (detail.lastSaveError) setSaved("failed");
      else if (!detail.dirty) setSaved("saved");
      setCanonicalStatus(detail.canonicalDirty === false ? "current" : "dirty");
    };
    const unsubscribe = WrDocs.subscribe((kind, detail) => {
      if (kind === "loaded") onDocLoaded(detail);
      else if (kind === "state") onDocState(detail);
    });
    /* 离开这个场景（或卸载）时，把未落盘的改动用「当时的」场景 id 冲掉 */
    const sid = activeScene;
    return () => {
      unsubscribe();
      if (typeof cleanupAfterLoad === "function") cleanupAfterLoad();
      clearTimeout(saveTimer.current);
      if (dirtyRef.current && sid && !wrSceneIsApproved(sid)) {
        const inFlight = inFlightRef.current.get(sid) || null;
        if (inFlight && inFlight.editVersion === editVersionRef.current) {
          /* 保存还在路上、之后没再敲字：不再叠一次同样的正文——叠上去的那次在它 409 时会绕过冲突副本、
             带着刷新后的修订号把另一台设备的正文盖掉。它断网 / 服务端出错失败时，同一部作品下补发一次。 */
          const workId = currentWorkId();
          void inFlight.done.then((outcome) => {
            if (outcome !== "failed" || currentWorkId() !== workId) return;
            try { void WrDocs.save(sid, inFlight.html).catch(() => {}); } catch (e) {}
          });
        } else {
          try { beforeSaveEvent(el, sid); } catch (e) {}
          try { void WrDocs.save(sid, wrSerializeManuscript(el)).catch(() => {}); } catch (e) {}
        }
        // 不等回包的离场冲刷：先按本机计数更新目录，回包的 words_rollup 到了再以服务端为准
        try { WsCatalog.recordSceneWords(sid, wrCountText(el)); } catch (e) {}
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
      setSaved("saving");
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
    if (!exactCodesMatch(exactCodes, acceptedCodes)) {
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
