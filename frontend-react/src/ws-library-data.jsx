import {
  LIB_BY_ID, LIB_CATS, LIB_ENTRIES, LIB_KIND_LABEL, LIB_KIND_OPTIONS, libLive, libLoadState, libRefetch, libSnapshot, libSubscribe,
} from "./ws-library-store.js";

/* ==========================================================
   资料库读取的门面 + 窗口接缝（过渡期）
   store 在 ws-library-store.js（读、写、快照、按需拉取都在那里，不写 window）。这个文件只做两件事：
   · 照旧转出资料页与单测 import 的名字；
   · 把 LIB_ENTRIES / LIB_BY_ID / LIB_CATS 挂到 window 上——写作台的名字高亮与悬停卡（ws-writer-entities.jsx）
     和 E2E 冒烟 smoke-phase6 还在读它们（章节编排的视角候选已改读 store 的 useLibraryLive）。它们改成 import store
     之后（window 接缝整体退役，见 Q1b），这个文件连同 ws-library-edit.jsx 一起删掉。
   ========================================================== */

Object.assign(window, { LIB_CATS, LIB_ENTRIES, LIB_BY_ID });

export { LIB_CATS, LIB_ENTRIES, LIB_BY_ID, LIB_KIND_LABEL, LIB_KIND_OPTIONS, libLive, libSubscribe, libSnapshot, libLoadState, libRefetch };
