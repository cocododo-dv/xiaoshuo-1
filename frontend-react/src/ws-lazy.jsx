import React from "react";

/* 可重试的懒加载入口（外壳路由与侧栏里的懒加载小部件共用）。
   React.lazy 会把一次失败的加载永久缓存下来，于是「重试」只会再抛同一个错；
   这里每个入口记住自己是否失败过，retryFailedRoutes() 只为失败的那几个重建 lazy 包装，
   已经载入的页面不受影响（不会再闪一次加载态）。纯 ESM，不写 window。 */
const wsLazyRoutes = new Set();

function lazyNamed(loader, exportName) {
  let failed = false;
  const make = () => React.lazy(async () => {
    try {
      const module = await loader();
      const component = module[exportName];
      if (!component) throw new Error(`模块缺少导出：${exportName}`);
      return { default: component };
    } catch (error) {
      failed = true;
      throw error;
    }
  });
  let Lazy = make();
  function LazyRoute(props) { return <Lazy {...props} />; }
  LazyRoute.retry = () => {
    if (!failed) return;
    failed = false;
    Lazy = make();
  };
  wsLazyRoutes.add(LazyRoute);
  return LazyRoute;
}

function retryFailedRoutes() {
  wsLazyRoutes.forEach((route) => route.retry());
}

export { lazyNamed, retryFailedRoutes };
