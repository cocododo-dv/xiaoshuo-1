import React from "react";

/* ==========================================================
   store-utils — store 层三类样板的机械收敛（行为等价，无新语义）
   · createSubscribers()：Set 订阅者 + 吞异常广播
   · useStoreTick(subscribe)：force-render hook 内核
   · storeAlert(error, fallback)：失败提示（外壳挂了提示层时走应用内提示，否则 window.alert）
   注意：各 store 的 CustomEvent 双通道广播（ws:work-changed 等）
   不在此收敛，仍留在各自 notify 内。
   ========================================================== */

/* 订阅者集合：subscribe(fn) 加入并返回退订函数；notify() 逐个调用，
   单个订阅者抛错被吞掉，不阻断其余订阅者与后续流程。 */
export function createSubscribers() {
  const subs = new Set();
  return {
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
    notify() { subs.forEach((fn) => { try { fn(); } catch (e) {} }); },
  };
}

/* force-render hook 内核：挂载时 subscribe(tick)，每次通知触发重渲；
   subscribe 返回退订函数（或 undefined）作为 effect 清理。 */
export function useStoreTick(subscribe) {
  const [, force] = React.useState(0);
  React.useEffect(() => subscribe(() => force((n) => n + 1)), []);
}

/* 失败提示的去处。外壳的提示层（ws-notify.jsx 的 WsToastHost）挂载时登记一个接收函数，
   store 的失败提示就显示成应用内的提示条，不再弹浏览器的阻塞对话框；
   没有登记（单测里单独渲染某个视图、提示层还没挂上）时仍走 window.alert——
   约二十个单测靠监视 window.alert 断言失败路径。这里只存一个函数，store-utils 不依赖任何 UI 模块。 */
let alertSink = null;

export function registerAlertSink(fn) {
  alertSink = typeof fn === "function" ? fn : null;
  return () => { if (alertSink === fn) alertSink = null; };
}

/* 失败提示样板：显示 (error && error.message) || fallback；
   error 传 null 即固定文案。alert 不可用（无头环境等）时静默。 */
export function storeAlert(error, fallback) {
  const message = (error && error.message) || fallback;
  if (alertSink) {
    try { if (alertSink(message) !== false) return; } catch (e) {}
  }
  try { window.alert(message); } catch (e) {}
}
