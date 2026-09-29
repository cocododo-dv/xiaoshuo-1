import { useReviewUrgent } from "./ws-review-store.js";

/* 侧栏待办徽标：只数当前作品「紧急（priority 1）」的未处理项，还没读到 / 没有紧急的不显示。
   读的是待办 store 的缓存（ws-review-store.js）——过去徽标自己另拉一遍 open 列表，作者写作时与 store
   各拉一次（审计 F01-08）。何时重读由 store 决定：换作品 / 回收站 / 别处投递去抖后拉，目录与雪花的
   自动保存按 20 秒节流合并成一次。 */
function useReviewBadge() {
  const urgent = useReviewUrgent();
  return urgent ? String(urgent) : null;
}

export { useReviewBadge };
