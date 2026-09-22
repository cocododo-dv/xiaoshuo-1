import React from "react";
import { WsCatalog } from "./ws-catalog.jsx";
import {
  wrDeepMark, wrDeepUnmark, wrDxAddSkip, wrDxApplyPreferences, wrDxFetch, wrDxLoadPreferences,
  wrDxMergePreferences, wrDxPushLog, wrDxRemoveSkip, wrDxRunAi, wrDxSavePreferences, wrDxSkips,
  wrDxSnapshot, wrDxWithIgnored,
} from "./ws-deep.jsx";
import { wrRangeForOffsets, wrRangeForText } from "./ws-writer-manuscript.js";
import { useWrEvent } from "./ws-writer-hooks.js";

/* ==========================================================
   深改姿态（2026-09-21 从 WriterRoom 拆出；2026-09-22 诊断改为服务端一份）
   ----------------------------------------------------------
   起草 / 深改是写作台的两种姿态。深改只诊断、不改字：正文只读，诊断从服务端取
   （GET /api/v1/scenes/{id}/deep-review：规则体检 + 段落节奏 + 起草评审 + AI 深评，同一种发现形状），
   按发现的段落序号与证据在正文里标高亮；每一项给「选中这一句去改写」/「按诊断改写」——回到起草姿态、
   选中那一句，改写走选区工具条，并把这条发现带给后端（rewriteFinding）。
   忽略 / 恢复按 signal_id 记：本机写穿 + 服务端 deep-review 偏好（带修订号，冲突时合并重试）；
   文学质量视图和成稿门读的是同一份忽略清单。
   进入深改时有未落盘的改动先存（正文马上变只读，诊断要对着存下去的字）；没改过就不存。
   从文学质量 / 待办 / 成稿中心带着 signal_id 跳进来：诊断到了就选中那一条并滚过去；
   当前作者稿里没有这一条时给一句提示（handoffMiss）。
   ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useMemo, useRef, useState } = React;

async function resolveBackendId(sid) {
  try {
    const resolved = WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
    return resolved || sid;
  } catch (e) {
    return sid;
  }
}

function shortIssue(finding) {
  const label = finding.label || finding.dimension || "";
  const issue = String(finding.issue || "").replace(/\s+/g, " ").trim();
  const text = issue.length > 40 ? issue.slice(0, 40) + "…" : issue;
  return label && text ? `${label}：${text}` : (label || text);
}

/* doc：useDocBinding 的返回；onEnter：进入深改时调用（打开右栏）；
   onLeave：「选中这一句去改写」离开深改前调用（收起叠放的抽屉） */
export function useDeepPosture({ activeScene, approvedLocked, editorRef, scrollRef, doc, onEnter, onLeave }) {
  const [posture, setPostureState] = useState("draft");
  const [diagnosis, setDiagnosis] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [skipTick, setSkipTick] = useState(0);
  const [activeKey, setActiveKey] = useState(null);
  const [filter, setFilter] = useState("all");
  const [showIgnored, setShowIgnored] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState(null);
  const [handoffMiss, setHandoffMiss] = useState(false);
  const [rewriteFinding, setRewriteFinding] = useState(null);
  const [log, setLog] = useState([]);
  const [persistenceStatus, setPersistenceStatus] = useState("idle");
  const prefsRef = useRef({ sceneId: null, revisionNo: 0, saveChain: Promise.resolve() });
  const sceneRef = useRef(activeScene);
  const pendingSignalRef = useRef(null);
  const loadSeq = useRef(0);
  useEffect(() => { sceneRef.current = activeScene; }, [activeScene]);

  /* 发现列表：服务端的诊断 + 本机的忽略清单（忽略 / 恢复不必等往返） */
  const findings = useMemo(
    () => (diagnosis && activeScene ? wrDxWithIgnored(diagnosis.findings, wrDxSkips(activeScene)) : []),
    [diagnosis, activeScene, skipTick], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const openCount = findings.filter((f) => !f.ignored).length;

  /* 终稿锁定的场不进深改；锁定发生时退回起草。带 signalId 进来：诊断到了就选中那一条 */
  const setPosture = useWrEvent((next, options = {}) => {
    const signalId = options && options.signalId ? String(options.signalId) : null;
    const wantDeep = next === "deep" && activeScene && !approvedLocked;
    if (signalId && wantDeep) {
      if (posture === "deep" && diagnosis) focusSignal(signalId);
      else pendingSignalRef.current = signalId;
    }
    setPostureState(wantDeep ? "deep" : "draft");
  });
  useEffect(() => {
    if (approvedLocked && posture === "deep") setPostureState("draft");
  }, [approvedLocked, posture]);

  const locateBlock = useWrEvent((block, behavior = "smooth") => {
    const scroller = scrollRef.current;
    if (!block || !scroller) return;
    const top = block.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 140;
    scroller.scrollTo({ top: Math.max(0, top), behavior });
  });
  const locatePara = useWrEvent((pid, behavior = "smooth") => {
    const el = editorRef.current;
    if (!el) return;
    locateBlock(el.querySelectorAll("p, blockquote")[pid], behavior);
  });
  const locateFinding = useWrEvent((finding) => {
    const el = editorRef.current;
    if (!el || !finding) return;
    let target = null;
    try { target = el.querySelector(`[data-dx="${CSS.escape(finding.signal_id)}"]`); } catch (e) { target = null; }
    if (target) {
      const block = target.matches("p, blockquote") ? target : target.closest("p, blockquote");
      locateBlock(block || target);
      return;
    }
    if (finding.evidence && Number.isInteger(finding.evidence.paragraph_index)) locatePara(finding.evidence.paragraph_index);
  });

  const focusSignal = useWrEvent((signalId) => {
    const hit = findings.find((f) => f.signal_id === signalId);
    if (!hit) { setHandoffMiss(true); return; }
    setHandoffMiss(false);
    setFilter("all");
    if (hit.ignored) setShowIgnored(true);
    setActiveKey(signalId);
    /* 标注在下一次渲染里画出来，滚动等它一拍 */
    setTimeout(() => locateFinding(hit), 60);
  });

  const queuePersist = useWrEvent((sceneId) => {
    if (!sceneId) return;
    const queuedSnapshot = wrDxSnapshot(sceneId);
    let state = prefsRef.current;
    if (state.sceneId !== sceneId) {
      state = { sceneId, revisionNo: 0, saveChain: Promise.resolve() };
      prefsRef.current = state;
    }
    setPersistenceStatus("saving");
    state.saveChain = state.saveChain
      .catch(() => undefined)
      .then(async () => {
        /* 服务端按后端场景 id 存；本机键仍按写作台的 sid（两者在乐观创建的场上会不同） */
        const backendId = await resolveBackendId(sceneId);
        let snapshot = queuedSnapshot;
        let result;
        try {
          result = await wrDxSavePreferences(backendId, snapshot, state.revisionNo);
        } catch (err) {
          if (err?.code !== "SCENE_DEEP_REVIEW_PREFERENCES_CONFLICT") throw err;
          const remote = await wrDxLoadPreferences(backendId);
          snapshot = wrDxMergePreferences(remote, queuedSnapshot, { localIgnoredAuthoritative: true });
          wrDxApplyPreferences(sceneId, snapshot);
          result = await wrDxSavePreferences(backendId, snapshot, remote.revision_no);
        }
        state.revisionNo = result.revision_no;
        if (prefsRef.current === state && sceneRef.current === sceneId) {
          wrDxApplyPreferences(sceneId, result);
          setLog(result.decision_log || []);
          setSkipTick((t) => t + 1);
          setPersistenceStatus("synced");
        }
      })
      .catch(() => {
        if (prefsRef.current === state && sceneRef.current === sceneId) setPersistenceStatus("local");
      });
  });

  /* 诊断载荷到了：存起来，把服务端的偏好与本机合并（本机多出来的决定补推上去），处理带进来的 signalId */
  const applyPayload = useWrEvent((sceneId, payload) => {
    setDiagnosis(payload || null);
    setLoading(false);
    setError(null);
    const remote = (payload && payload.preferences) || { decision_log: [], ignored_issue_keys: [], revision_no: 0 };
    let state = prefsRef.current;
    if (state.sceneId !== sceneId) {
      state = { sceneId, revisionNo: 0, saveChain: Promise.resolve() };
      prefsRef.current = state;
    }
    state.revisionNo = Number(remote.revision_no || 0);
    const local = wrDxSnapshot(sceneId);
    const merged = wrDxMergePreferences(remote, local);
    wrDxApplyPreferences(sceneId, merged);
    setLog(merged.decision_log);
    setSkipTick((t) => t + 1);
    const remoteSnapshot = { decision_log: remote.decision_log || [], ignored_issue_keys: remote.ignored_issue_keys || [] };
    if (JSON.stringify(merged) !== JSON.stringify(remoteSnapshot)) queuePersist(sceneId);
    else setPersistenceStatus("synced");
    const list = wrDxWithIgnored((payload && payload.findings) || [], new Set(merged.ignored_issue_keys));
    const wanted = pendingSignalRef.current;
    pendingSignalRef.current = null;
    if (wanted) {
      const hit = list.find((f) => f.signal_id === wanted);
      if (hit) {
        setHandoffMiss(false);
        setFilter("all");
        if (hit.ignored) setShowIgnored(true);
        setActiveKey(wanted);
        setTimeout(() => locateFinding(hit), 60);
      } else {
        setHandoffMiss(true);
        setActiveKey(null);
      }
    } else {
      /* 没指定就停在第一条还开着的发现上（面板展开它，正文里它的标注高亮） */
      const first = list.find((f) => !f.ignored);
      setActiveKey((key) => (list.some((f) => f.signal_id === key) ? key : (first ? first.signal_id : null)));
    }
  });

  const load = useWrEvent(async (sceneId) => {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    const backendId = await resolveBackendId(sceneId);
    try {
      const payload = await wrDxFetch(backendId);
      if (seq !== loadSeq.current || sceneRef.current !== sceneId) return;
      applyPayload(sceneId, payload);
    } catch (err) {
      if (seq !== loadSeq.current || sceneRef.current !== sceneId) return;
      setLoading(false);
      setError(err || new Error("diagnosis unavailable"));
      setPersistenceStatus("local");
    }
  });

  /* 进入深改：先冲掉未落盘的改动（诊断要对着存下去的字），再取诊断；退出：清标注 */
  useEffect(() => {
    const el = editorRef.current;
    if (!el) return undefined;
    let cancelled = false;
    if (posture === "deep" && activeScene) {
      const sceneId = activeScene;
      setDiagnosis(null);
      setError(null);
      setAiError(null);
      setHandoffMiss(false);
      setFilter("all");
      setShowIgnored(false);
      setLog(wrDxSnapshot(sceneId).decision_log);
      setPersistenceStatus("loading");
      if (onEnter) onEnter();
      (async () => {
        if (doc.dirtyRef.current) { doc.cancelPendingSave(); await doc.persistDoc(); }
        if (cancelled) return;
        await load(sceneId);
      })();
    } else {
      wrDeepUnmark(el);
      setDiagnosis(null);
      setActiveKey(null);
      setLoading(false);
      setPersistenceStatus("idle");
    }
    return () => { cancelled = true; };
  }, [posture, activeScene]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 换场：上一场的改写发现作废 */
  useEffect(() => { setRewriteFinding(null); }, [activeScene]);

  /* 诊断结果 / 选中项 / 忽略清单变化 → 重画正文标注 */
  useEffect(() => {
    const el = editorRef.current;
    if (el && posture === "deep") wrDeepMark(el, findings, activeKey);
  }, [findings, activeKey, posture, editorRef]);

  const pick = useWrEvent((key) => {
    setActiveKey(key);
    const finding = findings.find((item) => item.signal_id === key);
    if (finding) setTimeout(() => locateFinding(finding), 60);
  });
  const ignore = useWrEvent((finding) => {
    if (!activeScene || !finding) return;
    wrDxAddSkip(activeScene, finding.signal_id);
    setLog(wrDxPushLog(activeScene, `忽略 · ${shortIssue(finding)}`));
    setSkipTick((t) => t + 1);
    queuePersist(activeScene);
    if (activeKey === finding.signal_id) {
      const next = findings.find((item) => !item.ignored && item.signal_id !== finding.signal_id);
      setActiveKey(next ? next.signal_id : null);
    }
  });
  const restore = useWrEvent((finding) => {
    if (!activeScene || !finding) return;
    wrDxRemoveSkip(activeScene, finding.signal_id);
    setLog(wrDxPushLog(activeScene, `恢复 · ${shortIssue(finding)}`));
    setSkipTick((t) => t + 1);
    queuePersist(activeScene);
    setActiveKey(finding.signal_id);
  });
  const rescan = useWrEvent(() => { if (activeScene) load(activeScene); });
  const toggleIgnored = useWrEvent(() => setShowIgnored((v) => !v));

  const runAi = useWrEvent(async () => {
    if (!activeScene || aiBusy) return;
    const sceneId = activeScene;
    setAiBusy(true);
    setAiError(null);
    const backendId = await resolveBackendId(sceneId);
    try {
      const payload = await wrDxRunAi(backendId);
      if (sceneRef.current !== sceneId) return;
      const score = payload && payload.ai && payload.ai.overall_score != null ? Math.round(payload.ai.overall_score * 100) : null;
      setLog(wrDxPushLog(sceneId, score != null ? `AI 深评 · 总分 ${score}` : "AI 深评"));
      applyPayload(sceneId, payload);
      queuePersist(sceneId);
    } catch (err) {
      if (sceneRef.current !== sceneId) return;
      setAiError(err || new Error("deep review failed"));
    } finally {
      if (sceneRef.current === sceneId) setAiBusy(false);
    }
  });

  /* 「选中这一句去改写」：深改只诊断、不代笔。回到起草姿态，把那一句（段落级问题就是整段）选中，
     选区工具条随之出现——润色 / 更凝练 / 自定义…都在那里，走的是同一条改写接口。
     target 是一条发现（带 evidence）或工具条给的原始选区 { pid, find, start, end }。
     带发现时把它交给工具条（rewriteFinding）：改写请求会带上发现的 id / 维度 / 改法；
     autoRun：「按诊断改写」——选中之后工具条直接按发现的改法出候选，不必再点一次。
     偏移对不上（字变了）才按文字找第一处。叠放的抽屉先收起（onLeave），选区不能压在遮罩底下。 */
  const selectForRewrite = useWrEvent((target, { autoRun = false } = {}) => {
    if (approvedLocked || !target) return;
    const isFinding = !!target.signal_id;
    const ev = isFinding ? target.evidence : null;
    if (isFinding && !ev) return;
    const pid = isFinding ? ev.paragraph_index : target.pid;
    const find = isFinding ? (ev.excerpt || "") : (target.find || "");
    const start = isFinding ? ev.start : target.start;
    const end = isFinding ? ev.end : target.end;
    setRewriteFinding(isFinding ? { ...target, autoRun } : null);
    if (onLeave) onLeave();
    setPostureState("draft");
    const select = () => {
      const el = editorRef.current;
      if (!el) return;
      const blocks = el.querySelectorAll("p, blockquote");
      let block = blocks[pid];
      let range = null;
      if (block) {
        /* 先定位（瞬时滚动）再选中：选区工具条按选中那一刻的位置摆，之后再滚它就悬在别处 */
        locatePara(pid, "instant");
        if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
          range = wrRangeForOffsets(block, start, end);
          if (range && find && range.toString() !== find) range = null;
        }
        if (!range) range = wrRangeForText(block, find || "");
      }
      if (!range && find) {
        for (const candidate of Array.from(blocks)) {
          const hit = wrRangeForText(candidate, find);
          if (hit) { block = candidate; range = hit; locateBlock(candidate, "instant"); break; }
        }
      }
      if (!range) { setRewriteFinding(null); return; }
      el.focus({ preventScroll: true });
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    };
    /* 等姿态切换的这一轮渲染落定；右栏在 1000–1279 宽时要从停靠收回叠放，稿纸随之变宽重排——
       等这段过渡放完再量段落位置，否则滚到的是重排前的位置 */
    setTimeout(() => {
      const scroller = scrollRef.current;
      const running = scroller && scroller.getAnimations ? scroller.getAnimations() : [];
      if (!running.length) { select(); return; }
      Promise.all(running.map((anim) => anim.finished.catch(() => null))).then(select);
    }, 80);
  });
  const rewriteFromFinding = useWrEvent((finding) => selectForRewrite(finding, { autoRun: true }));
  const clearRewriteFinding = useWrEvent(() => setRewriteFinding(null));

  return {
    posture, setPosture, diagnosis, findings, openCount, activeKey, setActiveKey,
    filter, setFilter, showIgnored, toggleIgnored, loading, error, reload: rescan,
    aiBusy, aiError, runAi, handoffMiss, log, persistenceStatus,
    pick, ignore, restore, rescan, selectForRewrite, rewriteFromFinding, rewriteFinding, clearRewriteFinding,
  };
}
