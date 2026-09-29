/* 单测公共装配（vitest.config.js 的 dom 项目 setupFiles，每个测试文件加载前执行一次）。
   · 打开 React 18 的 act 环境：组件测试都用 act() 包更新，不开它 React 会报「not configured to support act」。
   · 每个用例结束后清空 localStorage / sessionStorage，用例之间不串数据。
   各测试文件里手写的同样几行（约 40 个文件）由各视图包在改到时删掉；重复设置无害。 */
import { afterEach } from "vitest";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  try { window.localStorage.clear(); } catch (e) { /* 存储被禁用时照常结束用例 */ }
  try { window.sessionStorage.clear(); } catch (e) { /* 同上 */ }
});
