import React from "react";
import {
  wrDeepMark, wrDeepScan, wrDeepUnmark, wrDxAddSkip, wrDxApplyPreferences, wrDxClearSkips,
  wrDxLoadPreferences, wrDxMergePreferences, wrDxPushLog, wrDxSavePreferences, wrDxSnapshot,
} from "./ws-deep.jsx";
import { wrRangeForOffsets, wrRangeForText } from "./ws-writer-manuscript.js";
import { useWrEvent } from "./ws-writer-hooks.js";

/* ==========================================================
   深改姿态（2026-09-21 从 WriterRoom 拆出）
   ----------------------------------------------------------
   起草 / 深改是写作台的两种姿态。深改只诊断、不改字：正文只读，本机启发式规则
   标出问题，每一项给「选中这一句去改写」——回到起草姿态、选中那一句，改写走选区工具条。
   忽略 / 重新诊断的决定按场存本机，并同步到服务端的 deep-review 偏好（带修订号，冲突时合并重试）。
   进入深改时有未落盘的改动先存（正文马上变只读）；没改过就不存，免得凭空多出一版草稿。
   ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useRef, useState } = React;

/* doc：useDocBinding 的返回；onEnter：进入深改时调用（打开右栏）；
   onLeave：「选中这一句去改写」离开深改前调用（收起叠放的抽屉） */
export function useDeepPosture({ activeScene, approvedLocked, editorRef, scrollRef, doc, onEnter, onLeave }) {
  const [posture, setPostureState] = useState("draft");
  const [issues, setIssues] = useState([]);
  const [activeKey, setActiveKey] = useState(null);
  const [log, setLog] = useState([]);
  const [persistenceStatus, setPersistenceStatus] = useState("idle");
  const prefsRef = useRef({ sceneId: null, revisionNo: 0, saveChain: Promise.resolve() });
  const sceneRef = useRef(activeScene);
  useEffect(() => { sceneRef.current = activeScene; }, [activeScene]);

  /* 终稿锁定的场不进深改；锁定发生时退回起草 */
  const setPosture = useWrEvent((next) => {
    setPostureState(next === "deep" && activeScene && !approvedLocked ? "deep" : "draft");
  });
  useEffect(() => {
    if (approvedLocked && posture === "deep") setPostureState("draft");
  }, [approvedLocked, posture]);

  const rescan = useWrEvent(() => {
    const el = editorRef.current;
    if (!el || !activeScene) return;
    const found = wrDeepScan(el, activeScene);
    setIssues(found);
    setActiveKey((key) => (found.some((issue) => issue.key === key) ? key : (found[0] ? found[0].key : null)));
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
        let snapshot = queuedSnapshot;
        let result;
        try {
          result = await wrDxSavePreferences(sceneId, snapshot, state.revisionNo);
        } catch (error) {
          if (error?.code !== "SCENE_DEEP_REVIEW_PREFERENCES_CONFLICT") throw error;
          const remote = await wrDxLoadPreferences(sceneId);
          snapshot = wrDxMergePreferences(remote, queuedSnapshot, { localIgnoredAuthoritative: true });
          wrDxApplyPreferences(sceneId, snapshot);
          result = await wrDxSavePreferences(sceneId, snapshot, remote.revision_no);
        }
        state.revisionNo = result.revision_no;
        if (prefsRef.current === state && sceneRef.current === sceneId) {
          wrDxApplyPreferences(sceneId, result);
          setLog(result.decision_log || []);
          setPersistenceStatus("synced");
        }
      })
      .catch(() => {
        if (prefsRef.current === state && sceneRef.current === sceneId) setPersistenceStatus("local");
      });
  });

  /* 进入深改：先冲掉未落盘的改动，再诊断并拉服务端的决定；退出：清标注 */
  useEffect(() => {
    const el = editorRef.current;
    if (!el) return undefined;
    let cancelled = false;
    if (posture === "deep" && activeScene) {
      if (doc.dirtyRef.current) { doc.cancelPendingSave(); void doc.persistDoc(); }
      const sceneId = activeScene;
      const local = wrDxSnapshot(sceneId);
      const state = { sceneId, revisionNo: 0, saveChain: Promise.resolve() };
      prefsRef.current = state;
      setLog(local.decision_log);
      setPersistenceStatus("loading");
      rescan();
      if (onEnter) onEnter();
      wrDxLoadPreferences(sceneId)
        .then((remote) => {
          if (cancelled || prefsRef.current !== state) return;
          state.revisionNo = remote.revision_no;
          const merged = wrDxMergePreferences(remote, local);
          wrDxApplyPreferences(sceneId, merged);
          setLog(merged.decision_log);
          rescan();
          const remoteSnapshot = {
            decision_log: remote.decision_log || [],
            ignored_issue_keys: remote.ignored_issue_keys || [],
          };
          if (JSON.stringify(merged) !== JSON.stringify(remoteSnapshot)) queuePersist(sceneId);
          else setPersistenceStatus("synced");
        })
        .catch(() => {
          if (!cancelled && prefsRef.current === state) setPersistenceStatus("local");
        });
    } else {
      wrDeepUnmark(el);
      setIssues([]);
      setPersistenceStatus("idle");
    }
    return () => { cancelled = true; };
  }, [posture, activeScene]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 诊断结果 / 选中项变化 → 重画正文标注 */
  useEffect(() => {
    const el = editorRef.current;
    if (el && posture === "deep") wrDeepMark(el, issues, activeKey);
  }, [issues, activeKey, posture, editorRef]);

  const locatePara = useWrEvent((pid, behavior = "smooth") => {
    const el = editorRef.current;
    const scroller = scrollRef.current;
    if (!el || !scroller) return;
    const block = el.querySelectorAll("p, blockquote")[pid];
    if (!block) return;
    const top = block.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 140;
    scroller.scrollTo({ top: Math.max(0, top), behavior });
  });

  const pick = useWrEvent((key) => {
    setActiveKey(key);
    const issue = issues.find((item) => item.key === key);
    if (issue) locatePara(issue.pid);
  });
  const ignore = useWrEvent((issue) => {
    wrDxAddSkip(activeScene, issue.key);
    setLog(wrDxPushLog(activeScene, `忽略 · ${issue.title}`));
    queuePersist(activeScene);
    rescan();
  });
  const rescanAll = useWrEvent(() => {
    wrDxClearSkips(activeScene);
    queuePersist(activeScene);
    rescan();
  });

  /* 「选中这一句去改写」：深改只诊断、不代笔。回到起草姿态，把那一句（段落级问题就是整段）选中，
     选区工具条随之出现——润色 / 更凝练 / 自定义…都在那里，走的是同一条改写接口。
     start / end（可选）：这一段里按拼接文字算的偏移——深改工具条「回起草改这句」带着它，
     同一段里重复出现的句子也能选回作者选的那一处；偏移对不上（字变了）才按文字找第一处。
     叠放的抽屉先收起（onLeave），选区不能压在遮罩底下。 */
  const selectForRewrite = useWrEvent(({ pid, find, start, end }) => {
    if (approvedLocked) return;
    if (onLeave) onLeave();
    setPostureState("draft");
    const select = () => {
      const el = editorRef.current;
      if (!el) return;
      const block = el.querySelectorAll("p, blockquote")[pid];
      if (!block) return;
      /* 先定位（瞬时滚动）再选中：选区工具条按选中那一刻的位置摆，之后再滚它就悬在别处 */
      locatePara(pid, "instant");
      let range = null;
      if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
        range = wrRangeForOffsets(block, start, end);
        if (range && find && range.toString() !== find) range = null;
      }
      if (!range) range = wrRangeForText(block, find || "");
      if (!range) return;
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

  return {
    posture, setPosture, issues, activeKey, setActiveKey, log, persistenceStatus,
    pick, ignore, rescanAll, selectForRewrite,
  };
}
