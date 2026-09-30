import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "./lib/client.js";
import { useWindowEvents } from "./lib/events.js";
import { announceDiagnosisChanged } from "./ws-diagnosis-summary.jsx";

/* ==========================================================
   成稿中心 · 诊断页签的读写（从 ws-manuscripts-diagnosis.jsx 拆出，2026-10）
   ----------------------------------------------------------
   · GET  /api/v1/chapters/{id}/deep-review —— 这一章的诊断（服务端现拼：章级通读、各场计数、落到各场的发现）；
   · POST 同一路径 {scope: "all" | "changed"} —— 「AI 通读本章」。回包就是新的诊断，直接换上，不再紧跟一个 GET；
     回包带的 diagnosis_rollup 随 ws:diagnosis-changed 广播出去（计数 store WsDiagnosis 在那里收下，只收一次）。
   · 页签开着时，别处的诊断变了（写作台保存、深评、看这一处）就重读——只认与这一章有关的广播：
     带 chapterId / rollup.chapter_id 的看是不是这一章，带场的看是不是这一章的场；什么都没带的照旧重读；
     自己刚发出去的那一条不算。
   · 页签上的一切都属于正在看的那一章：换章之后、新章的诊断读回来之前不拿上一章的诊断顶着；
     一次通读（进行中、回包、提示、出错）只属于发起它的那一章——等模型时作者换了章，回包不换到别章的页签上，
     也不作废新章自己的读取；提示与出错只挂在那一章上。回包的广播照发（计数 store 照收），换回那一章时照常重读。
   视图（ws-manuscripts-diagnosis.jsx）只管画；请求都经 lib/client.js。
   ========================================================== */

const IDLE = { status: "idle", payload: null, error: null, chapterId: "" };
const RUN_IDLE = { scope: null, error: null, notice: null };

const deepReviewPath = (chapterId) => `/api/v1/chapters/${encodeURIComponent(chapterId)}/deep-review`;

/* 自己广播的那一条正在派发（emit 是同步的）：页签自己的监听不因此重读 */
let ownAnnouncements = 0;

async function runChapterDeepReview(chapterId, scope = "all") {
  const next = await apiPost(deepReviewPath(chapterId), { scope });
  ownAnnouncements += 1;
  try {
    announceDiagnosisChanged({ chapterId, rollup: next && next.diagnosis_rollup });
  } finally {
    ownAnnouncements -= 1;
  }
  return next;
}

/* 一条 ws:diagnosis-changed 与这一章有没有关系。chapter：目录章（backendId、scenes[].backendId / sid）。 */
export function diagnosisEventConcernsChapter(detail, chapter) {
  const d = detail || {};
  const chapterId = chapter && chapter.backendId;
  const eventChapter = d.chapterId || (d.rollup && d.rollup.chapter_id) || "";
  if (eventChapter) return eventChapter === chapterId;
  if (d.sceneId || d.sid) {
    return ((chapter && chapter.scenes) || []).some((scene) => scene && (
      (d.sceneId && scene.backendId === d.sceneId) || (d.sid && scene.sid === d.sid)));
  }
  return true;
}

/* 通读的回包：服务端说「上次通读之后没有场改过字」（没调模型）时给一句 */
function upToDateNotice(next) {
  const notice = next && next.notice;
  if (!notice || notice.code !== "CHAPTER_REVIEW_UP_TO_DATE") return null;
  return notice.message || "上次通读之后没有场改过字。";
}

/* 这一章的诊断：换章就重读；页签开着时，与这一章有关的诊断变动也重读（乱序回来的旧请求不盖新结果）。
   「AI 通读本章」也在这里：run(scope) 按章记进行中 / 提示 / 出错（不同的章各记各的）。
   返回 { status, payload, error, reload, run, running, runError, runNotice }——都是正在看的这一章的。 */
export function useChapterDiagnosis(chapter) {
  const chapterId = (chapter && chapter.backendId) || "";
  const [state, setState] = useState(IDLE);
  const [runs, setRuns] = useState({});
  const seq = useRef(0);
  const chapterRef = useRef(chapter);
  chapterRef.current = chapter;

  const load = useCallback(() => {
    const mine = ++seq.current;
    if (!chapterId) { setState(IDLE); return Promise.resolve(); }
    setState((prev) => (prev.chapterId === chapterId
      ? { ...prev, status: "loading", error: null }
      : { status: "loading", payload: null, error: null, chapterId }));
    return apiGet(deepReviewPath(chapterId))
      .then((payload) => { if (seq.current === mine) setState({ status: "ready", payload, error: null, chapterId }); })
      .catch((error) => { if (seq.current === mine) setState((prev) => ({ ...prev, status: "error", error })); });
  }, [chapterId]);

  useEffect(() => {
    load();
    return () => { seq.current += 1; };
  }, [load]);

  useWindowEvents({
    "ws:diagnosis-changed": (event) => {
      if (ownAnnouncements > 0) return;
      if (!diagnosisEventConcernsChapter(event && event.detail, chapterRef.current)) return;
      load();
    },
  });

  const run = useCallback(async (scope = "all") => {
    const id = chapterId;
    if (!id || (runs[id] && runs[id].scope)) return;
    setRuns((prev) => ({ ...prev, [id]: { ...RUN_IDLE, scope } }));
    let outcome;
    try {
      const next = await runChapterDeepReview(id, scope);
      /* 通读的回包就是新诊断——页签还开着这一章才换上，并作废这一章还在路上的旧读取 */
      if (chapterRef.current && chapterRef.current.backendId === id) {
        seq.current += 1;
        setState({ status: "ready", payload: next, error: null, chapterId: id });
      }
      outcome = { ...RUN_IDLE, notice: upToDateNotice(next) };
    } catch (error) {
      outcome = { ...RUN_IDLE, error: error || new Error("chapter deep review failed") };
    }
    setRuns((prev) => ({ ...prev, [id]: outcome }));
  }, [chapterId, runs]);

  /* 页签上只画正在看的这一章：上一章的诊断（换章后新读取还没回来时）不算 */
  const mine = state.chapterId === chapterId ? state : { ...IDLE, status: chapterId ? "loading" : "idle", chapterId };
  const current = runs[chapterId] || RUN_IDLE;
  return {
    status: mine.status,
    payload: mine.payload,
    error: mine.error,
    reload: load,
    run,
    running: current.scope,
    runError: current.error,
    runNotice: current.notice,
  };
}
