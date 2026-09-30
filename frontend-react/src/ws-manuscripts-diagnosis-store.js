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
   视图（ws-manuscripts-diagnosis.jsx）只管画；请求都经 lib/client.js。
   ========================================================== */

const IDLE = { status: "idle", payload: null, error: null };

const deepReviewPath = (chapterId) => `/api/v1/chapters/${encodeURIComponent(chapterId)}/deep-review`;

/* 自己广播的那一条正在派发（emit 是同步的）：页签自己的监听不因此重读 */
let ownAnnouncements = 0;

export async function runChapterDeepReview(chapterId, scope = "all") {
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

/* 这一章的诊断：换章就重读；页签开着时，与这一章有关的诊断变动也重读（乱序回来的旧请求不盖新结果）。
   返回 { status, payload, error, reload, setPayload }。 */
export function useChapterDiagnosis(chapter) {
  const chapterId = chapter && chapter.backendId;
  const [state, setState] = useState(IDLE);
  const seq = useRef(0);
  const chapterRef = useRef(chapter);
  chapterRef.current = chapter;

  const load = useCallback(() => {
    const mine = ++seq.current;
    if (!chapterId) { setState(IDLE); return Promise.resolve(); }
    setState((prev) => ({ ...prev, status: "loading", error: null }));
    return apiGet(deepReviewPath(chapterId))
      .then((payload) => { if (seq.current === mine) setState({ status: "ready", payload, error: null }); })
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

  /* 通读的回包就是新诊断：换上它，并作废还在路上的旧读取 */
  const setPayload = useCallback((payload) => {
    seq.current += 1;
    setState({ status: "ready", payload, error: null });
  }, []);

  return { ...state, reload: load, setPayload };
}
