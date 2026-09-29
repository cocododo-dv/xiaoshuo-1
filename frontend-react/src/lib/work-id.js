/* 「当前作品是不是一部真实作品」的唯一判定（2026-09-29 前端共享层）。纯函数，不 import store、不写 window。
   书架还没从后端回来时，WsWorks 先用一部 id 为 "__loading__" 的占位作品渲染（ws-works.jsx 的 wsLoadCache）；
   后端确认书架为空时当前作品的 id 是空串。各 store / 视图原来各写一遍 `id && id !== "__loading__"`。
   调用点照旧自己从 WsWorks.activeId() / useActiveWorkIdentity() 取 id 再交给这里——单测里 mock 掉的
   WsWorks 只需给出 activeId，判定仍走这份真实实现。 */

export const LOADING_WORK_ID = "__loading__";

/* 这个 id 是不是一部真实作品：非空，且不是书架加载中的占位 */
export function isRealWorkId(id) {
  return !!id && id !== LOADING_WORK_ID;
}

/* 真实作品 id 原样返回，占位 / 空值给 null */
export function realWorkId(id) {
  return isRealWorkId(id) ? id : null;
}
