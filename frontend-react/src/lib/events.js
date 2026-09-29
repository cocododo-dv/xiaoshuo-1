import { useEffect, useRef } from "react";

/* 窗口事件小工具（2026-09-29 前端共享层）：广播、React 里的订阅、模块级监听的跨重载去重。
   不 import 视图 / store；不写 window 上的具名全局（去重登记表见下面的说明）。 */

/* 在 window 上广播一个 CustomEvent。detail 省略时事件的 detail 是 null（与 new CustomEvent(type) 相同）。
   广播是尽力而为：没有 window / CustomEvent 的环境（node 单测）里静默——原来每个调用点各包一层
   try { … } catch (e) {}。注意 detail 在调用 emit 之前就算好了：算 detail 本身可能抛错的地方别改用它。 */
export function emit(type, detail) {
  try {
    window.dispatchEvent(detail === undefined ? new CustomEvent(type) : new CustomEvent(type, { detail }));
  } catch (e) { /* 没有 DOM 的环境：广播是尽力而为 */ }
}

/* 窗口事件订阅：{ 事件名: 处理函数 }。只按事件名挂一次，处理函数每次渲染换成最新的（不再随依赖反复拆装）。 */
export function useWindowEvents(handlers) {
  const ref = useRef(handlers);
  ref.current = handlers;
  const names = Object.keys(handlers).sort().join("|");
  useEffect(() => {
    const bound = (names ? names.split("|") : []).map(name => [name, (event) => {
      const handler = ref.current && ref.current[name];
      if (handler) handler(event);
    }]);
    bound.forEach(([name, fn]) => window.addEventListener(name, fn));
    return () => bound.forEach(([name, fn]) => window.removeEventListener(name, fn));
  }, [names]);
}

/* 模块级全局监听的跨重载去重。store 模块在加载时就往 window 上挂监听器（作品切换、回收站变化……）；
   HMR 或测试里的 vi.resetModules 会让模块重新执行，旧实例的监听器还挂着，不撤就会和新实例一起响应。
   过去四个模块各在 window 上留一个具名全局（__wsCatalogGlobalHandlers 等）记旧句柄；现在只有这里在
   全局对象上留一个 Symbol 键的登记表（不是具名全局，也不会被别的代码读到），按 owner 记清理函数。
   用法：模块在挂新监听器之前 retireModuleListeners(owner)，挂好之后 adoptModuleListeners(owner, 清理函数)。 */
const MODULE_LISTENERS = Symbol.for("xiaoshuo.moduleListeners");

function moduleListenerRegistry() {
  if (!globalThis[MODULE_LISTENERS]) globalThis[MODULE_LISTENERS] = new Map();
  return globalThis[MODULE_LISTENERS];
}

/* 执行并撤掉 owner 上一个模块实例登记的清理函数（没有就什么都不做）。 */
export function retireModuleListeners(owner) {
  const registry = moduleListenerRegistry();
  const cleanup = registry.get(owner);
  registry.delete(owner);
  if (cleanup) cleanup();
}

/* 登记本实例的清理函数：下一次 retireModuleListeners(owner) 时执行。 */
export function adoptModuleListeners(owner, cleanup) {
  moduleListenerRegistry().set(owner, cleanup);
}
