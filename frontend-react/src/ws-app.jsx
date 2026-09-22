import React from "react";
import { TweaksPanel } from "./tweaks-panel.jsx";
import { WsWorks, useActiveWorkIdentity } from "./ws-works.jsx";
import { WsPalette, usePaletteShortcut } from "./ws-palette.jsx";
import { GlobalTweaks, SceneTweaks, WriterTweaks } from "./ws-shell-tweaks.jsx";
import { ProjectRequired, ViewErrorBoundary, ViewLoading } from "./ws-view-boundary.jsx";
import { flushViewIntents, queueViewIntents } from "./ws-view-intents.js";
import { WsToastHost } from "./ws-notify.jsx";
import { Rail } from "./ws-rail.jsx";
import { lazyNamed, retryFailedRoutes } from "./ws-lazy.jsx";
import { usePrefs } from "./ws-prefs.js";
import {
  WS_PROJECT_SCOPED_VIEWS, WS_VIEW_ALIAS, WS_VIEW_LABELS, isAdvancedView, isKnownView,
} from "./ws-nav.js";

/* ==========================================================
   App — 外壳：hash 路由、懒加载页面、侧栏、命令面板、排版与舒适度面板、提示层。
   侧栏在 ws-rail.jsx，作品切换器在 ws-work-switcher.jsx，导航数据在 ws-nav.js，
   偏好 schema 在 ws-prefs.js，提示 / 确认层在 ws-notify.jsx。
   这里保留路由本身：hash 读写（pushState / replaceState）、别名重定向、懒加载入口与 ViewReady 握手。
   ========================================================== */

const { useState: useAS, useEffect: useAE } = React;

/* SnowSync 是雪花工作台与章节编排共同依赖的运行时能力。它仍按业务路由懒加载，
   但必须和使用 window.SnowSync 的页面一起装配；只加载视图会让所有带可选链的保存
   静默退化成本机态，而分章面板的直接调用则在运行时崩掉。 */
function lazySnowNamed(loader, exportName) {
  return lazyNamed(async () => {
    const [module] = await Promise.all([loader(), import("./ws-snow-sync.jsx")]);
    return module;
  }, exportName);
}

const LazyWsHome = lazyNamed(() => import("./ws-home.jsx"), "WsHome");
const LazyWsConstruct = lazySnowNamed(() => import("./ws-snow.jsx"), "WsConstruct");
const LazyWsReview = lazyNamed(() => import("./ws-review.jsx"), "WsReview");
const LazyWsStyleRef = lazyNamed(() => import("./ws-styleref.jsx"), "WsStyleRef");
const LazyWsLibrary = lazyNamed(() => import("./ws-library.jsx"), "WsLibrary");
const LazyWsTrash = lazyNamed(() => import("./ws-trash.jsx"), "WsTrash");
const LazyWsAuthor = lazySnowNamed(() => import("./ws-author.jsx"), "WsAuthor");
const LazyWsScene = lazyNamed(() => import("./ws-scene.jsx"), "WsScene");
const LazyWsManuscripts = lazyNamed(() => import("./ws-manuscripts.jsx"), "WsManuscripts");
const LazyWsQuality = lazyNamed(() => import("./ws-quality.jsx"), "WsQuality");
const LazyWsCost = lazyNamed(() => import("./ws-cost.jsx"), "WsCost");
const LazyWsSettings = lazyNamed(() => import("./ws-settings.jsx"), "WsSettings");
const LazyWriterRoom = lazyNamed(() => import("./ws-writer.jsx"), "WriterRoom");

function ViewReady({ view, children }) {
  useAE(() => {
    const schedule = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 0));
    const cancel = window.cancelAnimationFrame || window.clearTimeout;
    const handle = schedule(() => flushViewIntents(view, window, { onlyWhenReady: true }));
    return () => cancel(handle);
  }, [view]);
  return children;
}

const WS_THEME = { day: "light", dusk: "sepia", night: "dark" };

function App() {
  const [t, setTweak] = usePrefs();
  const [view, setView] = useAS("home");
  const [palette, setPalette] = useAS(false);
  const mode = t.mode === "advanced" ? "advanced" : "writer";
  // 只订阅「当前作品是谁」：字数统计回写不再让外壳和整棵视图树重渲。
  const work = useActiveWorkIdentity();

  useAE(() => { document.documentElement.setAttribute("data-theme", WS_THEME[t.theme] || "light"); }, [t.theme]);
  useAE(() => { document.title = `创作工作台 · ${work.title}`; }, [work.title]);

  // hash routing
  useAE(() => {
    const r = () => {
      let h = (location.hash || "#home").replace("#", "");
      if (WS_VIEW_ALIAS[h]) {
        const target = WS_VIEW_ALIAS[h];
        history.replaceState(null, "", "#" + target);
        if (h === "deepdesk") queueViewIntents(target, { type: "ws:writer-posture", detail: "deep" });
        h = target;
      }
      if (isKnownView(h)) {
        // 深链到高级页面时自动切到高级模式，侧栏与当前页保持一致
        if (isAdvancedView(h)) setTweak("mode", "advanced");
        setView(h);
      }
    };
    r();
    window.addEventListener("hashchange", r);
    return () => window.removeEventListener("hashchange", r);
  }, []);

  const go = (v, intents) => {
    const queued = Array.isArray(intents) ? [...intents] : (intents ? [intents] : []);
    if (WS_VIEW_ALIAS[v]) {
      const orig = v;
      v = WS_VIEW_ALIAS[v];
      if (orig === "deepdesk") queued.push({ type: "ws:writer-posture", detail: "deep" });
    }
    if (!isKnownView(v)) return false;
    queueViewIntents(v, queued);
    // navigating to an advanced view auto-reveals 高级 mode so the rail stays consistent
    if (isAdvancedView(v) && mode !== "advanced") setTweak("mode", "advanced");
    setView(v);
    if (location.hash !== "#" + v) history.pushState(null, "", "#" + v);
    setPalette(false);
    if (v === view && queued.length) {
      const schedule = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 0));
      schedule(() => flushViewIntents(v, window, { onlyWhenReady: true }));
    }
    return true;
  };

  // ⌘K / Ctrl K 命令面板（别的模态层开着时不开，见 ws-palette.jsx 的 usePaletteShortcut）
  usePaletteShortcut(setPalette);

  const run = (cmd) => {
    switch (cmd.type) {
      case "go": go(cmd.view); break;
      case "theme": setTweak("theme", cmd.value); break;
      case "mode": setTweak("mode", cmd.value); break;
      case "tweaks": window.dispatchEvent(new CustomEvent("ws:tweaks-open")); break;
      case "scene":
        go("writer", { type: "ws:writer-scene", detail: cmd.sceneId });
        break;
      case "writer-action":
        go("writer", { type: "ws:writer-action", detail: cmd.action });
        break;
      case "step":
        go("snowflake", { type: "ws:snow-step", detail: cmd.key });
        break;
      case "work":
        WsWorks.setActive(cmd.workId);
        go("home");
        break;
      case "new-work":
        window.dispatchEvent(new CustomEvent("ws:new-work"));
        break;
      default: break;
    }
  };

  const inWriter = view === "writer";

  const renderView = () => {
    if (WS_PROJECT_SCOPED_VIEWS.has(view)) {
      if (work.id === "__loading__") return <ViewLoading label="书架" reason="data" />;
      if (!work.id) {
        return (
          <ProjectRequired
            label={WS_VIEW_LABELS[view] || "这个页面"}
            onCreate={() => window.dispatchEvent(new CustomEvent("ws:new-work"))}
            onGoHome={() => go("home")}
          />
        );
      }
    }
    switch (view) {
      case "home":        return <LazyWsHome go={go} mode={mode} />;
      case "snowflake":   return <LazyWsConstruct go={go} />;
      case "review":      return <LazyWsReview go={go} />;
      case "styleref":    return <LazyWsStyleRef go={go} />;
      case "library":     return <LazyWsLibrary go={go} />;
      case "author":      return <LazyWsAuthor go={go} />;
      case "scene":       return <LazyWsScene go={go} t={t} />;
      case "manuscripts": return <LazyWsManuscripts go={go} />;
      case "quality":     return <LazyWsQuality go={go} />;
      case "cost":        return <LazyWsCost go={go} />;
      case "settings":    return <LazyWsSettings go={go} t={t} setTweak={setTweak} />;
      case "trash":       return <LazyWsTrash go={go} />;
      case "writer":      return <div className="ws-writer-mount"><LazyWriterRoom t={t} setTweak={setTweak} go={go} onExit={() => go("home")} /></div>;
      default:            return <LazyWsHome go={go} mode={mode} />;
    }
  };

  return (
    <div className="ws-app" data-motion={t.motion}>
      <Rail view={view} go={go} t={t} setTweak={setTweak} mode={mode} onPalette={() => setPalette(true)} />
      <div className="ws-rail-scrim" aria-hidden="true" />
      <main className={`ws-content ${inWriter ? "is-writer" : ""}`} key={view + "::" + work.id}>
        <ViewErrorBoundary resetKey={view + "::" + work.id} onGoHome={() => go("home")} onRetry={retryFailedRoutes}>
          <React.Suspense fallback={<ViewLoading label={WS_VIEW_LABELS[view] || "页面"} />}>
            <ViewReady view={view}>{renderView()}</ViewReady>
          </React.Suspense>
        </ViewErrorBoundary>
      </main>

      <WsPalette open={palette} onClose={() => setPalette(false)} run={run} theme={t.theme} />

      {/* 排版与舒适度：全局外观在哪都有；写作台 / AI 起草台再加上它们自己的选项 */}
      <TweaksPanel title="排版与舒适度">
        <GlobalTweaks t={t} setTweak={setTweak} />
        {view === "writer" && <WriterTweaks t={t} setTweak={setTweak} />}
        {view === "scene" && <SceneTweaks t={t} setTweak={setTweak} />}
        {view !== "writer" && view !== "scene" && (
          <p className="twk-note">在写作台或 AI 起草台里打开时，这里还有稿纸排版、专注和起草台的显示选项。</p>
        )}
      </TweaksPanel>

      <WsToastHost />
    </div>
  );
}

/* createRoot 挂载在 main.jsx；侧栏单测直接从 ws-rail.jsx 取 Rail。 */
export { App };
