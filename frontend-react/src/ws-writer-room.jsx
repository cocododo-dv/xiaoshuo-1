import React from "react";
import { emit, useWindowEvents } from "./lib/events.js";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { SceneDesignCard, planIntentsForScene, sdLoadCollapsed } from "./ws-scene-design.jsx";
import { useDesignSync } from "./ws-design-sync.jsx";
import { ContentSafetyReviewDialog } from "./wr-content-safety-review.jsx";
import { wsKey } from "./ws-works.jsx";
import { WrDeepDrawer } from "./ws-deep.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { wsToast } from "./ws-notify.jsx";
import { EmptyState, Notice, Spinner } from "./ws-ui.jsx";
import { WRITER_TWEAK_DEFAULTS } from "./ws-shell-tweaks.jsx";
import { setViewIntentTargetReady } from "./ws-view-intents.js";
import { sanitizeManuscriptHTML } from "./manuscript-html.js";
import { WR_EMPTY_DOC } from "./ws-writer-manuscript.js";
import { wrAnnoAnchoredIds, wrAnnoApply, wrAnnoLoad, wrAnnoRefresh, wrAnnoSave } from "./ws-writer-annotations.js";
import {
  useImmersionChrome, useRailResize, useWrCounter, useWrEvent, useWrLayout, useWriterShortcuts,
} from "./ws-writer-hooks.js";
import { useWrCatalog, useWrCatalogStatus, useWrSceneMeta, wrInitialScene, wrNeighbours } from "./ws-writer-catalog.js";
import { useCanonicalPromotion, useDocBinding } from "./ws-writer-doc.js";
import { useDeepPosture } from "./ws-writer-deep-posture.js";
import { WrEntityPop, WrMentionPicker, useWrEntities, useWrMention, wrHighlightEntities } from "./ws-writer-entities.jsx";
import { WrHeader, WrProgress } from "./ws-writer-header.jsx";
import { WrDock, WrEdgeTabs, WrNextCue } from "./ws-writer-dock.jsx";
import { WrOutline, useWrOutlineActions } from "./ws-writer-outline.jsx";
import { WrContext } from "./ws-writer-context.jsx";
import { WrTray } from "./ws-writer-tray.jsx";
import { WrInlineRewrite } from "./ws-writer-inline.jsx";
import { formatClockTime } from "./lib/format.js";

/* ==========================================================
   WriterRoom — 写作台
   ----------------------------------------------------------
   这个组件只负责把各部分拼起来：
     正文与自动保存（ws-writer-doc.js）· 权威正文提升（同上）· 深改姿态（ws-writer-deep-posture.js）
     大纲（ws-writer-outline.jsx）· 上下文栏（ws-writer-context.jsx）· 续写托盘（ws-writer-tray.jsx）
     选区改写 / 批注（ws-writer-inline.jsx，批注存本机：ws-writer-annotations.js）
     档案实体与 @ 引用（ws-writer-entities.jsx）· 顶栏（ws-writer-header.jsx）· 工具条（ws-writer-dock.jsx）
   外观读 Tweaks 的 ws_tweaks_v1（wrLayout / aiPlace / focus / ambient / typewriter / measure / fontSize / lineHeight）。
   ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useMemo, useRef, useState } = React;

/* 设计卡里这一场的视角人物（没有就是空串）——续写托盘的场景行用它，不编卡上没有的说法 */
function designPov(design) {
  const fact = design && design.facts ? design.facts.find((f) => f.k === "视角") : null;
  return fact && fact.v ? fact.v : "";
}

/* 只挪光标、不改正文的键：松开时才需要重新定当前段 / 看 @ 提示（改正文的键 onInput 已经处理过） */
const WR_CARET_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

function annoKeyFor(sceneId) {
  return sceneId ? wsKey("wr-anno:" + sceneId) : null;
}

export function WriterRoom({ t, setTweak, onExit, go }) {
  const tw = { ...WRITER_TWEAK_DEFAULTS, ...(t || {}) };
  const isDesk = tw.wrLayout === "desk";

  const [activeScene, setActiveScene] = useState(wrInitialScene);
  const { toast, show: showNotice, clear: clearNotice } = useUndoToast();
  const editorRef = useRef(null);
  const scrollRef = useRef(null);
  const counter = useWrCounter();

  /* ---- 目录：大纲形状 + 当前这一场的页头 ----
     FE-ALIGN P3：目录是后端异步装载的；冷启动直达写作台时 activeScene 可能是 null，目录就绪后选中在写场景。
     切换作品 / 目录重载时，旧作品的场景 id 绝不能继续留在编辑器里；命中就换成目录里现在的 sid
     （乐观创建时的临时 sid、旧深链里的位置式 sid 都经别名解析到同一场）。 */
  const refocusTarget = (prev) => {
    const kept = prev ? WsCatalog.sceneById(prev) : null;
    if (kept) return kept.scene.sid;
    const hit = WsCatalog.focusScene ? WsCatalog.focusScene() : WsCatalog.writingScene();
    return hit && hit.scene ? hit.scene.sid : null;
  };
  /* 目录这次通知没动到当前场（绝大多数时候：自动保存回写字数）就不排更新——空更新也会让整间写作台重渲染 */
  const keepOrRefocus = useWrEvent(() => {
    if (refocusTarget(activeScene) === activeScene) return;
    setActiveScene(refocusTarget);
  });
  const { chapters, refresh } = useWrCatalog(keepOrRefocus);
  const meta = useWrSceneMeta(chapters, activeScene);
  const design = meta.design;
  const activeChapter = chapters.find((chapter) => chapter.scenes.some((scene) => scene.id === activeScene));
  const approvedLocked = !!(activeChapter && activeChapter.state === "approved");
  const catalogStatus = useWrCatalogStatus();
  const catalogUnavailable = catalogStatus.pending && catalogStatus.failed;
  const catalogLoading = catalogStatus.pending && !catalogStatus.failed;
  const nav = useMemo(() => wrNeighbours(chapters, activeScene), [chapters, activeScene]);

  /* ---- 布局（两侧栏的停靠规则要看姿态，useWrLayout 在深改姿态之后调用） ---- */
  const { railL, railR, dragging, startRail, resetRail } = useRailResize();
  const [rightTab, setRightTab] = useState("scene");
  const [trayOpen, setTrayOpen] = useState(false);
  const [immersion, setImmersion] = useState(false);
  const chrome = useImmersionChrome(immersion);
  /* 「写到设计篇幅」提示：作者点了「稍后」只对这一场生效，换一场重新计 */
  const [nextDismissedSid, setNextDismissedSid] = useState(null);
  /* 正文上方的设计卡收起时，抽屉里给整张卡；展开时抽屉只补正文上方没有的那部分 */
  const [pageCardCollapsed, setPageCardCollapsed] = useState(sdLoadCollapsed);

  /* 成功类回执走外壳的提示层（没挂提示层时——例如单测——落到本视图自己的回执条） */
  const notify = useWrEvent((message, tone = "ok") => {
    if (!wsToast({ message, tone })) showNotice({ text: message, tone });
  });

  /* ---- 段落聚焦 / 打字机滚动 ----
     每敲一个字都会走这里：只改上一个当前段与这一个（过去每次把整篇稿子的段落挨个 toggle 一遍）。
     上一个当前段已经不在编辑器里（整段换过内容）时它的 class 随节点一起没了，不用管。 */
  const activeBlockRef = useRef(null);
  const updateActive = useWrEvent(() => {
    const el = editorRef.current;
    if (!el) return;
    const sel = window.getSelection();
    let node = sel && sel.anchorNode;
    if (!node || !el.contains(node)) return;
    while (node && node.parentNode !== el) node = node.parentNode;
    const prev = activeBlockRef.current;
    if (prev !== node) {
      if (prev && prev.parentNode === el) prev.classList.remove("is-active");
      activeBlockRef.current = node;
    }
    if (node && node.classList && !node.classList.contains("is-active")) node.classList.add("is-active");
    /* 回车拆段、粘贴本编辑器里复制出去的段落时，浏览器会把 is-active 一起复制过来：多出来的才整篇清一遍 */
    const marked = el.querySelectorAll(":scope > .is-active");
    if (marked.length > 1) marked.forEach((child) => { if (child !== node) child.classList.remove("is-active"); });
    const scroller = scrollRef.current;
    if (!scroller || !node) return;
    const typewriter = tw.typewriter;
    if (!immersion && !typewriter) return;
    const sr = scroller.getBoundingClientRect();
    // 打字机模式钉住光标所在的那一行；沉浸模式把当前段落放到中间
    let r = null;
    if (typewriter && sel.rangeCount) {
      const rect = sel.getRangeAt(0).cloneRange().getBoundingClientRect();
      if (rect && rect.height) r = rect;
    }
    if (!r) r = node.getBoundingClientRect();
    const factor = typewriter ? 0.42 : 0.5;
    const target = scroller.scrollTop + (r.top - sr.top) - sr.height * factor + r.height / 2;
    scroller.scrollTo({ top: Math.max(0, target), behavior: typewriter ? "auto" : "smooth" });
  });

  /* ---- 档案实体、批注、正文 ---- */
  const focusWritingScene = useWrEvent(() => setActiveScene((prev) => {
    const hit = WsCatalog.writingScene();
    return hit ? hit.scene.sid : prev;
  }));
  const entities = useWrEntities({ editorRef, scrollRef, onNeedScene: focusWritingScene });
  const annoKey = annoKeyFor(activeScene);
  /* 标注时记下「这一场用的是哪个存储键」：离开这一场时的冲刷拿它，换作品也不会写错作品 */
  const annoScopeRef = useRef({ sid: null, key: null });
  /* 上一次告诉批注页签的「正文里标得出来的批注」，变了才再发 ws:anno-change */
  const annoAnchoredRef = useRef("");

  const doc = useDocBinding({
    activeScene, editorRef, counter,
    decorate: (el) => {
      wrHighlightEntities(el);
      annoScopeRef.current = { sid: activeScene, key: annoKey };
      annoAnchoredRef.current = Array.from(wrAnnoApply(el, wrAnnoLoad(annoKey))).sort().join(",");
      emit("ws:anno-change", { sid: activeScene });
    },
    afterLoad: () => {
      const frame = requestAnimationFrame(updateActive);
      const cancelLocate = entities.locatePending();
      return () => { cancelAnimationFrame(frame); if (cancelLocate) cancelLocate(); };
    },
    /* 作者改了被批注的字：落盘前按正文里现存的标注更新批注锚点（批注本身存本机，不进正文） */
    beforeSave: (el, sceneId) => {
      const scope = annoScopeRef.current;
      const key = scope.sid === sceneId ? scope.key : annoKeyFor(sceneId);
      const list = wrAnnoLoad(key);
      if (!list.length) return;
      const next = wrAnnoRefresh(el, list);
      if (next !== list) wrAnnoSave(key, next);
      /* 作者删掉了被批注的字：标注跟着没了，批注页签要把那一条改成「找不到原文」（可删），
         不能还挂着一个点了没反应的「定位」 */
      const anchored = Array.from(wrAnnoAnchoredIds(el)).sort().join(",");
      if (annoAnchoredRef.current !== anchored) {
        annoAnchoredRef.current = anchored;
        emit("ws:anno-change", { sid: sceneId });
      }
    },
  });
  const canonical = useCanonicalPromotion({ activeScene, doc, notify });
  /* 深改进场打开诊断栏；「选中这一句去改写」回起草前先收起叠放的抽屉（layout 在下面才建，调用时已就绪） */
  const onDeepEnter = useWrEvent(() => layout.openRight());
  const onDeepLeave = useWrEvent(() => layout.closeOverlayRails());
  const deep = useDeepPosture({ activeScene, approvedLocked, editorRef, scrollRef, doc, onEnter: onDeepEnter, onLeave: onDeepLeave });
  const posture = deep.posture;
  const inDeep = posture === "deep";
  const layout = useWrLayout(isDesk, posture);

  const commitEdit = useWrEvent(() => {
    if (approvedLocked) return;
    doc.recount();
    doc.schedulePersist();
  });
  const mention = useWrMention({ editorRef, onInserted: commitEdit });

  const onInput = () => {
    if (approvedLocked) return;
    const el = editorRef.current;
    /* 全选删光后浏览器会把最后一个 <p> 也删掉：补回一个空段落，接下来敲的字仍落在段落里 */
    if (el && !el.firstElementChild && !String(el.textContent || "").trim()) {
      el.innerHTML = WR_EMPTY_DOC;
      const range = document.createRange();
      range.setStart(el.firstElementChild, 0);
      range.collapse(true);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    doc.recount();
    doc.schedulePersist();
    // 作者一碰「作为草稿插入」的那一段，它就成了作者自己的字（插进来的是几段就各算各的）
    if (el) {
      const anchor = window.getSelection().anchorNode;
      const node = anchor && anchor.nodeType === 1 ? anchor : anchor && anchor.parentElement;
      const merged = node && node.closest ? node.closest("p.is-merge") : null;
      if (merged && el.contains(merged)) merged.classList.remove("is-merge");
    }
    updateActive();
  };

  /* ---- AI 续写：打开、采纳 ----
     深改姿态正文只读，续写和终稿锁定一样按「不能写」处理：按钮置灰，⌘J / 命令面板打不开托盘，
     采纳也不会往只读的正文里追加段落（过去深改里照样能续写、采纳并自动保存）。 */
  const trayReturnRef = useRef(null);
  const openAI = useWrEvent(() => {
    if (!activeScene || approvedLocked) return;
    if (inDeep) { notify("深改只诊断、不改字。回到起草再续写。", "neutral"); return; }
    if (tw.aiPlace === "drawer") { setRightTab("ai"); layout.openRight(); return; }
    /* 托盘一打开就把焦点拿进提示框；记下原来在哪（正文里还记光标），关上时还回去 */
    if (!trayOpen) {
      const el = editorRef.current;
      const sel = window.getSelection();
      const range = el && sel && sel.rangeCount && el.contains(sel.getRangeAt(0).startContainer) ? sel.getRangeAt(0).cloneRange() : null;
      trayReturnRef.current = { active: document.activeElement, range };
    }
    setTrayOpen(true);
  });
  /* 关托盘（Esc、×、点遮罩）：焦点回到打开前的地方，没有就回正文——托盘收起后会被标 inert，
     焦点留在里面就掉到 <body> 上，作者接着敲的字哪儿也去不了 */
  const closeTray = useWrEvent(() => {
    setTrayOpen(false);
    const saved = trayReturnRef.current;
    trayReturnRef.current = null;
    const el = editorRef.current;
    const back = saved && saved.active && saved.active !== document.body && saved.active.isConnected
      && !(saved.active.closest && saved.active.closest(".wr-tray")) ? saved.active : el;
    if (!back || !back.focus) return;
    back.focus({ preventScroll: true });
    if (back === el && saved && saved.range && el.contains(saved.range.startContainer)) {
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(saved.range);
    }
  });
  useEffect(() => { if (inDeep) setTrayOpen(false); }, [inDeep]);

  /* 光标放到这一段末尾并把焦点还给正文：采纳 / 作为草稿插入之后，作者接着敲的字就落在新段后面 */
  const caretToEnd = (p) => {
    const el = editorRef.current;
    if (!el || !p || !p.isConnected) return;
    el.focus({ preventScroll: true });
    const range = document.createRange();
    range.selectNodeContents(p);
    range.collapse(false);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  };

  /* 采纳后留在正文里：续写是几段就追加几段 <p>（空白页上那个空段落先拿掉，免得正文以一个空行开头）。
     返回追加的段落；光标放在最后一段末尾。 */
  const appendParagraphs = (className, htmls) => {
    const el = editorRef.current;
    const list = (htmls || []).filter((html) => html);
    if (approvedLocked || inDeep || !el || !list.length) return [];
    if (el.children.length === 1 && !String(el.textContent || "").trim()) el.innerHTML = "";
    const added = list.map((html) => {
      const p = document.createElement("p");
      p.className = className;
      p.innerHTML = html;
      el.appendChild(p);
      return p;
    });
    trayReturnRef.current = null;
    setTrayOpen(false);
    if (!layout.dockRight) layout.closeRight();
    caretToEnd(added[added.length - 1]);
    doc.recount();
    doc.schedulePersist();
    return added;
  };
  const scrollToEnd = () => {
    const scroller = scrollRef.current;
    if (scroller) scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  };
  const adoptParagraphs = (htmls) => {
    const added = appendParagraphs("is-fresh", htmls);
    if (!added.length) return;
    requestAnimationFrame(() => {
      scrollToEnd();
      updateActive();
      setTimeout(() => added.forEach((p) => p.classList.remove("is-fresh")), 1600);
    });
  };
  /* 候选里的一段（已转义的 HTML）→ 可以进正文的段落 HTML */
  const candParagraphs = (cand) => ((cand && cand.paras) || []).map((html) => sanitizeManuscriptHTML(String(html).replace(/<\/?mark>/g, "")));
  /* 采纳选中的几句：texts 是按原来的段落拼好的纯文字（一段一条） */
  const adoptText = useWrEvent((texts) => {
    const list = (Array.isArray(texts) ? texts : [texts]).filter((text) => text);
    if (!list.length) return;
    adoptParagraphs(list.map((text) => {
      const holder = document.createElement("p");
      holder.textContent = text;
      return holder.innerHTML;
    }));
  });
  const adopt = useWrEvent((cand) => adoptParagraphs(candParagraphs(cand)));
  /* 作为草稿插入：插成待改的草稿段落并把光标放进去，让作者用自己的话揉进去，而不是原样收下。
     草稿段落的虚线框只在这次打开时可见（落盘的是干净正文）。 */
  const merge = useWrEvent((cand) => {
    const added = appendParagraphs("is-merge", candParagraphs(cand));
    if (!added.length) return;
    requestAnimationFrame(() => {
      scrollToEnd();
      caretToEnd(added[added.length - 1]);
      updateActive();
    });
  });

  /* ---- 快捷键与外部意图（命令面板 / 主页 / 章节编排 / 成稿中心跳来） ---- */
  useWriterShortcuts({
    onAI: () => { if (activeScene) openAI(); },
    onImmersion: () => setImmersion((v) => !v),
    onToggleLeft: layout.toggleLeft,
    onToggleRight: layout.toggleRight,
    /* 停靠的栏从不因 Esc 收起——只收托盘和叠放的抽屉，最后才退出沉浸 */
    onEscape: () => {
      if (trayOpen) closeTray();
      else if (layout.overlayOpen) layout.closeOverlayRails();
      else if (immersion) setImmersion(false);
    },
  });
  /* 从别处跳来：只收起叠放的抽屉，停靠的栏保持原样——不管从哪儿进来，写作台看起来都一样 */
  const onSceneIntent = useWrEvent((sid) => { setActiveScene(sid); layout.closeOverlayRails(); });
  const onActionIntent = useWrEvent((action) => {
    if (action === "ai") openAI();
    else if (action === "immersion") setImmersion((v) => !v);
    else if (action === "deep") deep.setPosture("deep");
  });
  /* 姿态意图的 detail：字符串（"deep" / "draft"），或 { posture, signal_id }——文学质量 / 待办 / 成稿中心
     带着一条发现跳进来，深改面板到了诊断就选中那一条并滚过去 */
  const onPostureIntent = useWrEvent((detail) => {
    const next = typeof detail === "string" ? detail : (detail && detail.posture);
    const signalId = detail && typeof detail === "object" ? (detail.signal_id || detail.signalId || null) : null;
    deep.setPosture(next === "deep" ? "deep" : "draft", { signalId });
  });
  /* 先挂监听、再报「写作台已就绪」：排队的跨页意图在报就绪时立即派发（两个 effect 按声明顺序执行） */
  useWindowEvents({
    "ws:writer-scene": (e) => { if (e.detail) onSceneIntent(e.detail); },
    "ws:writer-action": (e) => onActionIntent(e.detail),
    "ws:writer-posture": (e) => onPostureIntent(e.detail),
  });
  useEffect(() => {
    setViewIntentTargetReady("writer");
    return () => setViewIntentTargetReady("writer", false);
  }, []);

  /* ---- 大纲 ---- */
  const outline = useWrOutlineActions({ chapters, refresh, activeScene, setActiveScene, showNotice, go });
  const pickScene = useWrEvent((id) => { setActiveScene(id); if (!layout.dockLeft) layout.closeLeft(); });

  /* ---- 设计卡「待同步」：整页只挂一次 useDesignSync ---- */
  const designSync = useDesignSync();
  const backendId = design ? design.backendId : "";
  const syncPending = !!(backendId && designSync.pendingFor(backendId));
  const syncBusy = !!(backendId && designSync.isBusy(backendId));
  const sync = useMemo(
    () => (syncPending ? { pending: true, busy: syncBusy, onSync: () => designSync.syncScenes([backendId]) } : null),
    [syncPending, syncBusy, backendId, designSync],
  );

  const openSettings = useWrEvent(() => { if (go) go("settings", { type: "ws:settings-tab", detail: "ai" }); });
  const onOpenSettings = go ? openSettings : null;
  const editPlan = go && design ? () => go("snowflake", planIntentsForScene(design.backendId)) : null;
  const editCard = go ? () => go("author") : null;
  /* 抽屉里的设计卡：正文上方那张展开着就只补它没有的；收起了或在沉浸里，给整张 */
  const contextVariant = !pageCardCollapsed && !immersion ? "context" : "full";
  const lengthRange = design && design.lengthRange ? design.lengthRange : null;
  const { dockLeft, dockRight, leftOpen, rightOpen, overlayOpen } = layout;

  return (
    <div
      className="wr-root"
      data-screen-label="writer"
      data-focus={tw.focus}
      data-ambient={tw.ambient ? "on" : "off"}
      data-motion={t && t.motion ? t.motion : "standard"}
      data-texture={t && t.texture === false ? "off" : "on"}
      data-immersion={immersion ? "on" : "off"}
      data-chrome={chrome ? "show" : "hidden"}
      data-layout={isDesk ? "desk" : "immersive"}
      data-posture={posture}
      data-typewriter={tw.typewriter ? "on" : "off"}
      data-left={leftOpen ? "on" : "off"}
      data-right={rightOpen ? "on" : "off"}
      data-dock-left={dockLeft ? "on" : "off"}
      data-dock-right={dockRight ? "on" : "off"}
      data-dragging={dragging ? "on" : "off"}
      style={{ "--measure": tw.measure + "px", "--ms-fz": tw.fontSize + "px", "--ms-lh": String(tw.lineHeight), "--rail-l": railL + "px", "--rail-r": railR + "px" }}
    >
      <div className="wr-room-bg" />
      <div className="wr-room">
        <WrProgress counter={counter} lengthRange={lengthRange} />
        <WrHeader
          meta={meta}
          onOpenOutline={layout.openLeft}
          posture={posture}
          onPosture={deep.setPosture}
          deepIssueCount={deep.openCount}
          hasScene={!!activeScene}
          approvedLocked={approvedLocked}
          counter={counter}
          saved={doc.saved}
          canonicalStatus={doc.canonicalStatus}
          canonicalDisabled={!activeScene || approvedLocked || !!canonical.review}
          onPromote={canonical.promote}
        />

        <div className="wr-scroll" ref={scrollRef} onClick={updateActive}>
          {!activeScene && (catalogLoading ? (
            <div className="wr-blank" role="status">
              <Spinner size={16} /> 正在从服务端加载章节与正文…
            </div>
          ) : catalogUnavailable ? (
            <div className="wr-blank" role="status">
              <EmptyState icon="AlertTriangle" title="章节目录加载失败"
                actions={<button type="button" className="btn btn-ghost" onClick={() => WsCatalog.__refresh && WsCatalog.__refresh()}><I.Refresh size={14} /> 重试加载</button>}>
                系统不会把网络失败当成空作品。恢复连接后重试，正文与目录都不会被本地空状态覆盖。
              </EmptyState>
            </div>
          ) : (
            <div className="wr-blank">
              <EmptyState icon="BookOpen" title="这部作品还没有章节"
                actions={<>
                  <button type="button" className="btn btn-accent" onClick={outline.createFirstChapter}><I.Plus size={15} /> 创建第一章</button>
                  <button type="button" className="btn btn-ghost" onClick={() => { if (go) go("snowflake"); else window.location.hash = "#snowflake"; }}>去构思</button>
                </>}>
                建一个第一章和一场开场就能动笔；也可以先去雪花构思把结构长出来。
              </EmptyState>
            </div>
          ))}
          <div className="wr-measure" style={!activeScene ? { display: "none" } : undefined}>
            <header className="wr-scene-head">
              <div className="wr-stamp">{meta.stamp}</div>
              <h1 className="wr-scene-title">{meta.title}</h1>
              {design
                ? <SceneDesignCard model={design} variant="compact" sync={sync}
                    onCollapsedChange={setPageCardCollapsed} onEditPlan={editPlan} onEditCard={editCard} />
                : <div className="wr-goal"><span className="wr-goal-k">本场目标</span><span className="wr-goal-v">{meta.goal}</span></div>}
              {approvedLocked && <div className="wr-final-lock" role="status"><I.Lock size={13} /> 已批准终稿只读。需要改写时，请先到成稿中心重新打开本章。</div>}
              {posture === "deep" && (
                <Notice tone="info" className="wr-deep-note" icon={I.Microscope}>
                  深改只诊断、不改字：正文此刻只读。点正文里的高亮看诊断；要改，就在右栏把那一句（或那一段）选中，带回起草去改。
                </Notice>
              )}
            </header>
            {/* 选区工具条画在 DOM 末尾，Tab 走不过去：键盘入口是 Alt+F10（ws-writer-inline.jsx），在这里说一声 */}
            <span id="wr-editor-kbd-hint" className="ws-sr-only">选中文字后按 Alt+F10 进入改写工具条。</span>
            <div className="wr-editor" ref={editorRef} role="textbox" aria-multiline="true"
              aria-label={`${meta.title || "当前场景"}正文编辑区`} aria-readonly={posture === "deep" || approvedLocked} tabIndex={0}
              aria-describedby={approvedLocked ? undefined : "wr-editor-kbd-hint"}
              contentEditable={posture !== "deep" && !approvedLocked} suppressContentEditableWarning spellCheck={false}
              onInput={() => { onInput(); mention.detectMention(); }}
              onKeyDown={mention.onMentionKeyDown}
              onKeyUp={(e) => {
                /* 敲字、删字已经由 onInput 处理过；这里只管只挪光标的键 */
                if (!WR_CARET_KEYS.has(e.key)) return;
                updateActive();
                mention.detectMention();
              }}
              onMouseOver={entities.onEditorOver} onMouseOut={entities.onEditorOut}
              onClick={(e) => {
                if (posture === "deep") {
                  const dx = e.target.closest && e.target.closest("[data-dx]");
                  if (dx) { deep.setActiveKey(dx.getAttribute("data-dx")); return; }
                }
                entities.onEditorClick(e);
              }} />
            <footer className="wr-scene-foot">
              <span className="wr-foot-meta">{approvedLocked ? "终稿锁定，只读" : (doc.savedAt ? `自动保存于 ${formatClockTime(doc.savedAt)}` : "自动保存已开启")}</span>
              <div className="wr-foot-nav">
                <button type="button" className="btn btn-ghost btn-sm" disabled={!nav.prevId} onClick={() => { if (nav.prevId) setActiveScene(nav.prevId); }}>
                  <I.ChevronLeft size={13} /> 上一场
                </button>
                <button type="button" className="btn btn-ghost btn-sm" disabled={!nav.nextId}
                  title={nav.nextTitle ? `下一场：${nav.nextTitle}` : "这是全书最后一场"}
                  onClick={() => { if (nav.nextId) setActiveScene(nav.nextId); }}>
                  下一场 <I.ChevronRight size={13} />
                </button>
              </div>
            </footer>
          </div>
        </div>

        <WrEdgeTabs onOpenLeft={layout.openLeft} onOpenRight={layout.openRight} />
        <WrDock
          onExit={onExit}
          leftOpen={leftOpen} rightOpen={rightOpen}
          onToggleLeft={layout.toggleLeft} onToggleRight={layout.toggleRight}
          hasScene={!!activeScene} approvedLocked={approvedLocked} deep={inDeep} onOpenAI={openAI}
          isDesk={isDesk} onToggleLayout={() => { if (setTweak) setTweak("wrLayout", isDesk ? "immersive" : "desk"); }}
          immersion={immersion} onToggleImmersion={() => setImmersion((v) => !v)}
        />

        {dockLeft && leftOpen && <div className="wr-resize wr-resize-l" onPointerDown={(e) => startRail("l", e)} onDoubleClick={() => resetRail("l")} title="拖动调整大纲栏宽，双击恢复默认" />}
        {dockRight && rightOpen && <div className="wr-resize wr-resize-r" onPointerDown={(e) => startRail("r", e)} onDoubleClick={() => resetRail("r")} title="拖动调整上下文栏宽，双击恢复默认" />}

        <WrNextCue counter={counter} lengthRange={lengthRange} nextTitle={nav.nextTitle}
          enabled={nextDismissedSid !== activeScene && !overlayOpen && !inDeep}
          onGo={() => { if (nav.nextId) setActiveScene(nav.nextId); }} onDismiss={() => setNextDismissedSid(activeScene)} />
      </div>

      <div className={`wr-scrim wr-scrim-drawer ${overlayOpen ? "show" : ""}`} onClick={layout.closeOverlayRails} />
      <WrOutline open={leftOpen} activeScene={activeScene} chapters={chapters}
        onReorder={outline.onReorder} onRename={outline.onRename} onDelete={outline.onDelete}
        onDeleteChapter={outline.onDeleteChapter} onDeleteBatch={outline.onDeleteBatch} onAdd={outline.onAdd}
        onPick={pickScene} onClose={layout.closeLeft} />
      {posture === "deep"
        ? <WrDeepDrawer deep={deep} open={rightOpen} onOpenSettings={onOpenSettings} onClose={layout.closeRight} />
        : <WrContext open={rightOpen} tab={rightTab} setTab={setRightTab} onClose={layout.closeRight} place={tw.aiPlace}
            tight={dockRight && railR < 232} sceneId={activeScene} design={design} designVariant={contextVariant} sync={sync} go={go}
            editorRef={editorRef} annoKey={annoKey} onAdopt={adopt} onMerge={merge} onAdoptText={adoptText} onOpenSettings={onOpenSettings} />}
      <div className={`wr-scrim wr-scrim-tray ${trayOpen ? "show" : ""}`} onClick={closeTray} />
      <WrTray open={trayOpen} onClose={closeTray} onAdopt={adopt} onMerge={merge} onAdoptText={adoptText}
        sceneId={activeScene} design={design} onOpenSettings={onOpenSettings}
        sceneLabel={meta.stamp ? meta.stamp + (meta.title ? `「${meta.title}」` : "") : null}
        pov={designPov(design)} />
      <UndoToast toast={toast} onClose={clearNotice} />
      <WrInlineRewrite editorRef={editorRef} sceneId={activeScene} annoKey={annoKey} onCommit={commitEdit}
        readOnly={approvedLocked} deep={posture === "deep"} onRewriteSelection={deep.selectForRewrite} onOpenSettings={onOpenSettings}
        onPassageReview={(slice) => deep.reviewPassage(
          Number.isInteger(slice.pidEnd) && slice.pidEnd > slice.pid
            ? { paragraph_start: slice.pid, paragraph_end: slice.pidEnd }
            : { paragraph_index: slice.pid, excerpt: slice.find },
        )} passageBusy={!!deep.passageBusy}
        finding={deep.rewriteFinding} onFindingDone={deep.clearRewriteFinding} />
      <WrEntityPop pop={entities.entityPop} />
      <WrMentionPicker mention={mention.mention} list={mention.mentionList} idx={mention.mentionIdx}
        onPick={mention.insertMention} onHover={mention.setMentionIdx} />
      {canonical.review && (
        <ContentSafetyReviewDialog
          review={canonical.review}
          busy={canonical.busy}
          error={canonical.error}
          onCancel={canonical.cancel}
          onConfirm={canonical.confirm}
        />
      )}
    </div>
  );
}
