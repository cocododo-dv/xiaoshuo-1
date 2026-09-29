import { realWorkId } from "./work-id.js";

/* store 能拿去发请求的「当前作品 id」（2026-09-29 前端数据层）。
   书架还在加载（占位作品）、后端确认书架为空、新建作品还在等后端给正式 id 时都是 null——
   WsWorks.readyId() 是这条规则本身（ws-works.jsx）。这里收一个 WsWorks 对象而不 import 它：lib 不依赖 store；
   单测里被 mock 的 WsWorks 常常只给 activeId，那时退回「是真实作品 id 就用」（mock 没有临时作品）。 */
export function readyWorkId(works) {
  try {
    if (works && typeof works.readyId === "function") return works.readyId();
    return realWorkId(works && works.activeId ? works.activeId() : null);
  } catch (e) {
    return null;
  }
}
