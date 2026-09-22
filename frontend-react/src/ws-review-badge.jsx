import React from "react";
import { apiGet } from "./lib/client.js";
import { WsWorks } from "./ws-works.jsx";

/* 待办徽标只数「紧急（priority 1）」的未处理项。下面这些信号都可能改变它，但频率差别很大：
   待办 / 作品 / 回收站的变化是作者的动作，去抖 180ms 就拉；
   目录与雪花的保存在写作时每次自动保存都会广播（字数回写也算），按 20 秒节流，
   窗口内的信号合并成窗口末尾的一次补拉——不丢变化，也不让自动保存每次多打一个请求。
   收件箱 store 自己广播的 ws:review-changed 带着它刚算好的紧急条数（detail.urgent）：
   直接用，不再为 store 自己的变化多拉一遍整张列表；不带条数的广播（别处投递）照旧补拉。 */
const REVIEW_REFRESH_EVENTS = [
  "ws:review-changed",
  "ws:work-changed",
  "ws:trash-changed",
];
const REVIEW_NOISY_EVENTS = ["ws:catalog-changed", "ws:snow-saved"];
const BADGE_DEBOUNCE_MS = 180;
const BADGE_NOISY_MIN_INTERVAL_MS = 20_000;

function useReviewBadge() {
  const [badge, setBadge] = React.useState(null);
  const requestVersion = React.useRef(0);

  React.useEffect(() => {
    let disposed = false;
    let timer = null;
    let trailing = null;
    let lastFetchAt = 0;
    let lastProjectId = WsWorks.activeId();

    const refresh = async () => {
      const projectId = WsWorks.activeId();
      const version = ++requestVersion.current;
      lastFetchAt = Date.now();
      if (!projectId || projectId === "__loading__") {
        setBadge(null);
        return;
      }
      try {
        const result = await apiGet(`/api/v1/review-items?state=open&project_id=${encodeURIComponent(projectId)}`);
        if (disposed || version !== requestVersion.current) return;
        const urgent = ((result && result.items) || []).filter((item) => Number(item.priority || 2) === 1).length;
        setBadge(urgent > 0 ? String(urgent) : null);
      } catch (ignored) {
        // 导航徽标是辅助信息；网络失败时保留上次结果，不阻断整个应用外壳。
      }
    };

    const scheduleRefresh = (event) => {
      const detail = event && event.type === "ws:review-changed" ? event.detail : null;
      if (detail && typeof detail.urgent === "number" && detail.projectId === WsWorks.activeId()) {
        window.clearTimeout(timer);
        requestVersion.current += 1; // 在途的旧请求作废：store 的计数更新
        lastFetchAt = Date.now();
        setBadge(detail.urgent > 0 ? String(detail.urgent) : null);
        return;
      }
      if (event?.type === "ws:work-changed") {
        // 只有真的换了作品才清掉上一部的徽标；书架成员变化（删了另一部）不该让它闪一下。
        const nextId = WsWorks.activeId();
        if (nextId !== lastProjectId) setBadge(null);
        lastProjectId = nextId;
      }
      window.clearTimeout(timer);
      window.clearTimeout(trailing);
      trailing = null;
      timer = window.setTimeout(() => { void refresh(); }, BADGE_DEBOUNCE_MS);
    };

    const scheduleNoisyRefresh = () => {
      const wait = lastFetchAt + BADGE_NOISY_MIN_INTERVAL_MS - Date.now();
      if (wait <= 0) {
        scheduleRefresh();
        return;
      }
      if (trailing) return;
      trailing = window.setTimeout(() => { trailing = null; void refresh(); }, wait);
    };

    REVIEW_REFRESH_EVENTS.forEach((name) => window.addEventListener(name, scheduleRefresh));
    REVIEW_NOISY_EVENTS.forEach((name) => window.addEventListener(name, scheduleNoisyRefresh));
    void refresh();
    return () => {
      disposed = true;
      requestVersion.current += 1;
      window.clearTimeout(timer);
      window.clearTimeout(trailing);
      REVIEW_REFRESH_EVENTS.forEach((name) => window.removeEventListener(name, scheduleRefresh));
      REVIEW_NOISY_EVENTS.forEach((name) => window.removeEventListener(name, scheduleNoisyRefresh));
    };
  }, []);

  return badge;
}

export { useReviewBadge };
