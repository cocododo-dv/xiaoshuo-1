import React from "react";
import { I } from "./icons.jsx";
import { CloseButton, EmptyState, Tabs } from "./ws-ui.jsx";
import { SceneDesignCard, planIntentsForScene } from "./ws-scene-design.jsx";
import { navigateWithViewIntent } from "./ws-view-intents.js";
import { wrAnnoAnchoredIds, wrAnnoFirstMark, wrAnnoLoad, wrAnnoSave } from "./ws-writer-annotations.js";
import { useWrInert } from "./ws-writer-hooks.js";
import { WrContinuePanel } from "./ws-writer-candidates.jsx";
import { WrCtxNotes } from "./ws-writer-notes.jsx";

/* ==========================================================
   场景上下文栏（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   页签：戏剧（这一场的设计卡）· AI（只在「AI 放在抽屉里」时）· 批注 · 笔记。
   React.memo：敲字不再让这一栏跟着重渲染；批注数按 ws:anno-change 事件刷新，
   不再靠每秒一次的整树重渲染「顺便」读 DOM。ESM 模块，不写 window。
   ========================================================== */

const { memo, useEffect, useRef, useState } = React;

/* 批注清单：存在本机浏览器；正文里标得出来的点一下定位，找不到原文的可以删 */
function WrAnnoList({ editorRef, annoKey, sceneId }) {
  const list = wrAnnoLoad(annoKey);
  const anchored = wrAnnoAnchoredIds(editorRef && editorRef.current);
  const announce = () => window.dispatchEvent(new CustomEvent("ws:anno-change", { detail: { sid: sceneId } }));
  const jump = (id) => {
    const editor = editorRef.current;
    const mark = wrAnnoFirstMark(editor, id);
    const scroller = editor && editor.closest(".wr-scroll");
    /* 标注已经不在正文里了（刚删掉那段字、还没到自动保存）：重画清单，这一条改成「找不到原文」 */
    if (!mark) { announce(); return; }
    if (!scroller) return;
    const r = mark.getBoundingClientRect();
    const sr = scroller.getBoundingClientRect();
    scroller.scrollTo({ top: Math.max(0, scroller.scrollTop + (r.top - sr.top) - sr.height / 2 + r.height / 2), behavior: "smooth" });
    mark.classList.add("wr-anno-flash");
    setTimeout(() => mark.classList.remove("wr-anno-flash"), 1200);
  };
  const remove = (id) => {
    wrAnnoSave(annoKey, wrAnnoLoad(annoKey).filter((anno) => anno.id !== id));
    announce();
  };
  if (!list.length) {
    return (
      <EmptyState compact icon="Edit" title="本场还没有批注" className="wr-anno-empty">
        选中正文，在弹出的工具条里点「批注」。批注保存在本机浏览器里，不进正文。
      </EmptyState>
    );
  }
  return (
    <div className="wr-anno-list">
      <div className="wr-block-h">本场批注（{list.length}）</div>
      <p className="wr-anno-scope">保存在本机浏览器里，不进正文，也不会同步到服务器或其他设备。点正文里的黄色标注可以改。</p>
      {list.map((anno) => (anchored.has(anno.id) ? (
        <button type="button" key={anno.id} className="wr-anno-item" onClick={() => jump(anno.id)}>
          <span className="wr-anno-item-q">{anno.quote}</span>
          <span className="wr-anno-item-n">{anno.note || "（没有写批注内容）"}</span>
        </button>
      ) : (
        <div key={anno.id} className="wr-anno-item is-lost">
          <span className="wr-anno-item-q">{anno.quote}</span>
          <span className="wr-anno-item-n">{anno.note || "（没有写批注内容）"}</span>
          <span className="wr-anno-item-lost">
            正文改过了，找不到这段文字。
            <button type="button" className="btn btn-quiet btn-xs" onClick={() => remove(anno.id)}>删除这条批注</button>
          </span>
        </div>
      )))}
    </div>
  );
}

/* 戏剧页签：与 AI 起草台预检里是同一张设计卡。正文上方那张展开时，这里只补它没有的部分（variant="context"） */
function WrCtxScene({ sceneId, design, variant, sync, go }) {
  if (!design) {
    return (
      <EmptyState compact icon="Compass" title="还没有这一场的设计卡">
        目录还在装载，或者这一场不在目录里。
      </EmptyState>
    );
  }
  /* 写作台 → AI 起草台的直达动线：这一场在目录里存在时，单场入列并跳转 */
  const forkAI = () => {
    const intent = { type: "ws:scene-enqueue", detail: { sid: sceneId } };
    if (go) go("scene", [intent]); else navigateWithViewIntent("scene", intent.type, intent.detail);
  };
  /* 回构思第 10 步并对准这一场（与成稿中心「回第 10 步」同一组意图）；跨视图一律走 go(view, intents) */
  const toPlan = () => {
    const intents = planIntentsForScene(design.backendId);
    if (go) go("snowflake", intents);
    else navigateWithViewIntent("snowflake", intents[0].type, intents[0].detail);
  };
  const toCard = () => { if (go) go("author"); else window.location.hash = "#author"; };
  return (
    <section className="wr-block">
      <SceneDesignCard model={design} variant={variant} sync={variant === "context" ? null : sync} onEditPlan={toPlan} onEditCard={toCard} />
      <button type="button" className="btn btn-quiet btn-sm wr-fork-ai" onClick={forkAI}
        title="把这一场交给 AI 起草台：按这张设计卡与雪花上下文起草整场，归档后写回这里的正文">
        <I.Play size={13} /> 交给 AI 起草整场
      </button>
    </section>
  );
}

function WrContextImpl({
  open, tab, setTab, onClose, place, tight, sceneId, design, designVariant, sync, go,
  editorRef, annoKey, onAdopt, onMerge, onAdoptText, onOpenSettings,
}) {
  // 批注增删、换场重新标注后都会广播 ws:anno-change：这一栏据此重读批注数与清单
  const [, setAnnoTick] = useState(0);
  useEffect(() => {
    const bump = () => setAnnoTick((n) => n + 1);
    window.addEventListener("ws:anno-change", bump);
    return () => window.removeEventListener("ws:anno-change", bump);
  }, []);
  const annoCount = wrAnnoLoad(annoKey).length;
  /* AI 页签只在「AI 放在抽屉里」时出现；放在托盘时，续写只有托盘这一个入口 */
  const aiInDrawer = place === "drawer";
  const current = tab === "ai" && !aiInDrawer ? "scene" : tab;
  const tabs = [
    { id: "scene", label: "戏剧" },
    ...(aiInDrawer ? [{ id: "ai", label: "AI" }] : []),
    { id: "anno", label: "批注", count: annoCount || null },
    { id: "notes", label: "笔记" },
  ];
  const asideRef = useRef(null);
  useWrInert(asideRef, !open);
  return (
    <aside ref={asideRef} className={`wr-drawer right ${open ? "show" : ""}`} aria-label="场景上下文">
      <header className="wr-drawer-head">
        <I.Compass size={16} /><span className="wr-drawer-title">场景上下文</span>
        <CloseButton className="wr-drawer-x" label="收起上下文" onClick={onClose} />
      </header>
      <Tabs className={`wr-tabs ${tight ? "is-tight" : ""}`} label="场景上下文分类" idPrefix="wr-ctx"
        value={current} onChange={setTab} tabs={tabs} />
      <div className="wr-drawer-body" role="tabpanel" id={`wr-ctx-panel-${current}`} aria-labelledby={`wr-ctx-tab-${current}`}>
        {current === "scene" && <WrCtxScene sceneId={sceneId} design={design} variant={designVariant} sync={sync} go={go} />}
        {current === "ai" && <WrContinuePanel sceneId={sceneId} design={design} onAdopt={onAdopt} onMerge={onMerge} onAdoptText={onAdoptText} onOpenSettings={onOpenSettings} />}
        {current === "anno" && <WrAnnoList editorRef={editorRef} annoKey={annoKey} sceneId={sceneId} />}
        {current === "notes" && <WrCtxNotes scene={sceneId} />}
      </div>
    </aside>
  );
}

export const WrContext = memo(WrContextImpl);
