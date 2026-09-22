/* ==========================================================
   AI 起草台 — 运行引擎的对外门面
   ----------------------------------------------------------
   2026-09-21 拆分：原来这一个文件里同时有网络请求、localStorage、纯推导和三个 React 组件。
   现在各归其位，这里只把名字原样转出去——单测与 scripts/smoke-f6.mjs 仍从这里 import，
   页面自己直接引用各模块（不经过门面，免得门面与页面互相牵连）。
   · ws-scene-derive.js   纯推导：状态词、七步进度、风格提示、裁决投影、workbench → 运行记录
   · ws-scene-store.js    本机持久化：scn-run:<sid> / scn-queue:v1 / scn-queue-dismissed:v1
   · ws-scene-api.js      与后端说话：起草、恢复、终选、预算追加、归档
   · ws-scene-job.jsx     任务控制条（唯一的轮询者）
   · ws-scene-stage.jsx   风格链路提示条（与台面其它部分）
   · ws-scene-evidence.jsx 本场参考窗口（与证据栏其它部分）
   ========================================================== */

export {
  STYLE_NOTICE_LABELS, RUN_STAGES, runJobStepLabel, scnRunStageIndex, scnDraftModeFrom,
  scnStyleNoticesFrom, scnStyleWindowsFrom, scnStyleNoticeLabel, scnStyleWindowLabel,
  scnTerminalJobMessage, scnQC, scnReQC, scnFindingText, scnFindingIsPlainLanguage,
  scnGateFrom, scnRewriteBriefFrom, scnRunRecordFromWorkbench,
} from "./ws-scene-derive.js";
export {
  scnRunLoad, scnRunSave, scnQueueLoad, scnQueueSave, scnQueueDismissLoad, scnQueueDismissAdd, scnQueueDismissClear,
} from "./ws-scene-store.js";
export {
  scnRun, scnHydrateFromBackend, scnBackendQueueSids, scnBackendRunSids, scnTopupBudget,
  scnCandidates, scnSelectCandidate, scnResumeAfterSelection,
  scnAdoptToDoc, scnAdoptionPreview, scnPrepareAdoption,
} from "./ws-scene-api.js";
export { SceneRunJobControl } from "./ws-scene-job.jsx";
export { SceneStyleNoticeStrip } from "./ws-scene-stage.jsx";
export { SceneStyleWindowsPanel } from "./ws-scene-evidence.jsx";
