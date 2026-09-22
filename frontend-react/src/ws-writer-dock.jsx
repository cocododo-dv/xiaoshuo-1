import React from "react";
import { I } from "./icons.jsx";
import { modCombo } from "./ws-writer-keys.js";
import { useWrCount } from "./ws-writer-hooks.js";

/* ==========================================================
   写作台底部工具条、两侧的召唤条、「写到设计篇幅」提示（2026-09-21 从 ws-writer.jsx 拆出）
   ESM 模块，不写 window。
   ========================================================== */

/* deep：深改姿态正文只读，续写和终稿锁定一样置灰，并说清楚怎么回去写 */
export function WrDock({ onExit, leftOpen, rightOpen, onToggleLeft, onToggleRight, hasScene, approvedLocked, deep = false, onOpenAI, isDesk, onToggleLayout, immersion, onToggleImmersion }) {
  const aiBlocked = approvedLocked ? "终稿已锁定，请先重新打开章节" : deep ? "深改只诊断，回到起草再续写" : "";
  return (
    <div className="wr-dock" role="toolbar" aria-label="写作台工具">
      {onExit && (<><button type="button" className="wr-dock-btn wr-dock-icon" onClick={onExit} title="回主页" aria-label="回主页"><I.Home size={16} /></button><div className="wr-dock-sep" /></>)}
      <button type="button" className={`wr-dock-btn ${leftOpen ? "is-on" : ""}`} aria-pressed={leftOpen} onClick={onToggleLeft} title={`大纲（${modCombo("1")}）`}>
        <I.BookOpen size={16} /><span className="wr-dock-label">大纲</span>
      </button>
      <button type="button" className={`wr-dock-btn ${rightOpen ? "is-on" : ""}`} aria-pressed={rightOpen} onClick={onToggleRight} title={`上下文（${modCombo("2")}）`}>
        <I.Compass size={16} /><span className="wr-dock-label">上下文</span>
      </button>
      {hasScene && <div className="wr-dock-sep" />}
      {hasScene && (
        <button type="button" className="wr-dock-btn accent" disabled={!!aiBlocked} onClick={onOpenAI} title={aiBlocked || `AI 续写下一段（${modCombo("J")}）`}>
          <I.Sparkles size={16} /><span className="wr-dock-label">AI 续写</span><kbd>{modCombo("J")}</kbd>
        </button>
      )}
      {hasScene && <div className="wr-dock-sep" />}
      <button type="button" className={`wr-dock-btn wr-dock-icon ${isDesk ? "wr-layout-on" : ""}`} onClick={onToggleLayout}
        title={isDesk ? "切换到沉浸稿纸" : "切换到书桌三栏"} aria-label={isDesk ? "切换到沉浸稿纸" : "切换到书桌三栏"}><I.PanelLeft size={16} /></button>
      <button type="button" className={`wr-dock-btn wr-dock-icon ${immersion ? "is-on" : ""}`} aria-pressed={immersion} onClick={onToggleImmersion}
        title={`沉浸写作（${modCombo(".")}）`} aria-label="沉浸写作"><I.Eye size={16} /></button>
    </div>
  );
}

/* 两侧栏收起（叠放）时，贴边的召唤条 */
export function WrEdgeTabs({ onOpenLeft, onOpenRight }) {
  return (
    <>
      <button type="button" className="wr-edge left" onClick={onOpenLeft} title={`章节大纲（${modCombo("1")}）`}><span className="wr-edge-label">大纲</span></button>
      <button type="button" className="wr-edge right" onClick={onOpenRight} title={`场景上下文（${modCombo("2")}）`}><span className="wr-edge-label">上下文</span></button>
    </>
  );
}

/* 下一场的名字读目录里真的下一场（这里曾经写死着已退役演示作品的场名）；全书最后一场没有「下一场」，不出提示。
   只在这一场真的写到设计篇幅时浮出来；「稍后」只管这一场。enabled：外层条件（没被「稍后」、没有叠放抽屉挡着、不在深改——
   深改时它会压住稿纸底部的诊断提示，而且那时要做的是改这一场，不是去写下一场）。 */
export function WrNextCue({ counter, lengthRange, enabled, nextTitle, onGo, onDismiss }) {
  const count = useWrCount(counter);
  const show = !!enabled && !!nextTitle && !!lengthRange && count >= lengthRange.min;
  return (
    <div className={`wr-next ${show ? "show" : ""}`} role={show ? "status" : undefined} aria-hidden={show ? undefined : "true"}>
      <span className="wr-next-text">已写到设计篇幅。下一场：<b>{nextTitle}</b></span>
      <button type="button" className="wr-next-go" tabIndex={show ? 0 : -1} onClick={onGo}>写下一场 <I.ChevronRight size={13} /></button>
      <button type="button" className="wr-next-dismiss" tabIndex={show ? 0 : -1} onClick={onDismiss} title="稍后" aria-label="稍后再说"><I.X size={14} /></button>
    </div>
  );
}
