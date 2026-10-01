/* ==========================================================
   ws-test-seam — 契约级 E2E 冒烟拿 store 的唯一窗口接缝（只在开发态装）
   ----------------------------------------------------------
   store 全是 ES 模块，谁用谁 import；原型期挂在 window 上的那批运行时全局（WsWorks / WsCatalog / WrDocs /
   SnowSync / rv* / LIB_* …）已全部退役。React 契约 E2E（frontend-react/scripts/smoke-*.mjs，跑在 `npm run dev`
   起的开发服务器上，见 scripts/lib/harness.mjs）要直接驱动或读 store 时经这里拿：
     const { WsCatalog, WrDocs } = await window.__wsStores.load("WsCatalog", "WrDocs");
   · load 按名字动态 import 对应的模块，拿到的就是应用自己在用的那一个模块实例；还没加载的（写作台的正文 store、
     资料库的 store）这一下才加载——冒烟不用再等哪个页面先把它挂上 window（以前 smoke-phase6 就卡在这里）。
   · 只在 import.meta.env.DEV 时装。生产构建里这段连同里面的动态 import 一起被摇掉，产物的模块图不变；也因此
     这里一个 store 都不静态 import（静态 import 会把写作台、资料库的 store 拉进入口块）。
   · 这是唯一允许写 window 的模块（tooling-independence 的 KNOWN_WINDOW_WRITERS 只剩它）。名字表只放冒烟用得到的；
     单测直接 import 模块，不经这里。
   ========================================================== */

export function installTestSeam() {
  if (!import.meta.env.DEV || typeof window === "undefined") return;
  const loaders = {
    WsWorks: () => import("./ws-works.jsx").then((m) => m.WsWorks),
    WsCatalog: () => import("./ws-catalog.jsx").then((m) => m.WsCatalog),
    WrDocs: () => import("./wr-doc-store.jsx").then((m) => m.WrDocs),
    rvOpenItems: () => import("./ws-review-store.js").then((m) => m.rvOpenItems),
    libLive: () => import("./ws-library-store.js").then((m) => m.libLive),
    LIB_persist: () => import("./ws-library-store.js").then((m) => m.LIB_persist),
  };
  window.__wsStores = {
    names: Object.keys(loaders),
    async load(...names) {
      const stores = {};
      for (const name of names) {
        const loader = loaders[name];
        if (!loader) throw new Error(`window.__wsStores 里没有「${name}」（有：${Object.keys(loaders).join("、")}）`);
        stores[name] = await loader();
      }
      return stores;
    },
  };
}
