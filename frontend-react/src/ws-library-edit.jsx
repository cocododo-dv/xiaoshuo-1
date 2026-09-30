import { LIB_createEntry, LIB_deleteEntry, LIB_persist, libLive } from "./ws-library-store.js";
import { DossierCreate, DossierEdit } from "./ws-library-form.jsx";

/* ==========================================================
   资料库写入 / 表单的门面 + 窗口接缝（过渡期）
   写入在 ws-library-store.js，编辑 / 新建表单在 ws-library-form.jsx；这里照旧转出资料页 import 的名字，
   并把 window.LIB_persist（E2E 冒烟 smoke-phase6 用它改名再改回）与 window.LIB_live（写作台的名字高亮读它）
   挂上。它们改成 import store 之后，这个文件连同 ws-library-data.jsx 一起删掉。
   2026-09-30：浏览器里 7–8 月的旧资料一次性上行（LIB_migrateLegacy / LIB_persistAdds / LIB_newEntry）与
   只剩空壳的窗口名（LIB_loadEdits、LIB_applyEdit、LIB_loadAdds、LIB_seedOn……）删掉（批准 #25，重评 R16）；
   浏览器里的旧键原样留着，只是不再读。
   ========================================================== */

const LIB_live = libLive;

Object.assign(window, { LIB_persist, LIB_live });

export { LIB_persist, LIB_live, LIB_deleteEntry, LIB_createEntry, DossierEdit, DossierCreate };
