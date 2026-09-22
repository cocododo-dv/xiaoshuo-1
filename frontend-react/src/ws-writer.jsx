/* ==========================================================
   写作台入口
   ----------------------------------------------------------
   路由按 lazyNamed(() => import("./ws-writer.jsx"), "WriterRoom") 懒加载这里；
   写作台本身拆在 ws-writer-*.jsx|js 里（同一个 domain-writer 构建块），
   这里只转出路由、Tweaks 面板与单测要用的名字。ESM 模块，不写 window。
   ========================================================== */

export { WriterRoom } from "./ws-writer-room.jsx";
export { WriterTweaks, WRITER_TWEAK_DEFAULTS } from "./ws-shell-tweaks.jsx";
export { WrCtxNotes } from "./ws-writer-notes.jsx";
export { wrContinueMulti } from "./ws-writer-requests.js";
export { wrPickedText, wrPlainText, wrSentences } from "./writer-candidates.js";
