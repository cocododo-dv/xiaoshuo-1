import React from "react";
import { I } from "./icons.jsx";
import { ARR_ACTS } from "./ws-author-data.js";
import { arrChapterEdge, arrIsPlanChapter, arrIsPlanScene, arrRangeLabel } from "./ws-author-derive.js";
import { ArrAiArrange } from "./ws-author-ai.jsx";
import { ArrDramaCard } from "./ws-author-drama.jsx";
import { ArrSceneBoard } from "./ws-author-scenes.jsx";
import { ArrChapterStateTag, blurOnEnter } from "./ws-author-ui.jsx";
import { ArrChapterRunAction } from "./ws-chapter-run.jsx";
import { planIntentsForScene } from "./ws-scene-design.jsx";
import { writerIntents } from "./ws-view-intents.js";
import { IconButton, Notice, Tag } from "./ws-ui.jsx";
import { chapterLabel } from "./labels/catalog.js";

/* ==========================================================
   章节详情 — 中间的编辑器
   页头（面包屑 · 章名 · 状态 / 运行 / 删除 / 体检 · 锚点）→ 构思条 → 场景看板（ws-author-scenes.jsx）→ 交接 →
   戏剧卡（ws-author-drama.jsx）→ AI 编排（ws-author-ai.jsx）。
   跨视图的去处（写作台、AI 起草台、构思第 10 步）都走外壳给的 goView。
   ========================================================== */

const { useRef } = React;

/* ---- 章与章的交接 ----
   入口 / 出口从场上读：入口 = 第一场在做什么，出口 = 最后一场离场时变了什么——两章之间接不接得上，一眼看得出来。 */
function ArrHandoffStrip({ prev, ch, next, numOf, onJump, sectionRef }) {
  const entry = arrChapterEdge(ch, "entry");
  const exit = arrChapterEdge(ch, "exit");
  const prevExit = prev ? arrChapterEdge(prev, "exit") : "";
  const nextEntry = next ? arrChapterEdge(next, "entry") : "";
  return (
    <div className="arr-handoff" data-testid="arr-handoff" ref={sectionRef}>
      <button type="button" className={`arr-ho-cell arr-ho-side ${prev ? "" : "is-empty"}`} disabled={!prev} onClick={() => prev && onJump(prev.id)}>
        <span className="arr-ho-k"><I.ChevronLeft size={12} />{prev ? `承接第 ${Number(numOf[prev.id])} 章` : "全书开篇"}</span>
        <span className="arr-ho-text" title={prev ? prevExit : undefined}>{prev ? (prevExit || "上一章还没有出口") : "没有前一章"}</span>
      </button>
      <div className="arr-ho-cell arr-ho-mid">
        <span className="arr-ho-k">本章的入口和出口{(entry || exit) ? <em className="arr-ho-src" title="入口取第一场，出口取最后一场的离场变化">取自首尾两场</em> : null}</span>
        <span className="arr-ho-text arr-ho-entry" title={entry}><i className="arr-ho-tick">入</i><span className="arr-ho-clamp">{entry || "还没有场"}</span></span>
        <span className="arr-ho-text arr-ho-exit" title={exit}><i className="arr-ho-tick is-out">出</i><span className="arr-ho-clamp">{exit || "还没有场"}</span></span>
      </div>
      <button type="button" className={`arr-ho-cell arr-ho-side ${next ? "" : "is-empty"}`} disabled={!next} onClick={() => next && onJump(next.id)}>
        <span className="arr-ho-k">{next ? `交给第 ${Number(numOf[next.id])} 章` : "全书收束"}<I.ChevronRight size={12} /></span>
        <span className="arr-ho-text" title={next ? nextEntry : undefined}>{next ? (nextEntry || "下一章还没有场") : "没有后一章"}</span>
      </button>
    </div>
  );
}

/* ---- 构思条（阶段 Z）----
   这一章在构思里是什么——第几卷、收在哪个灾难上、装着故事序上第几到第几场、章摘要 / 章目标；
   以及两扇门：整理章节结构（就在这里开面板）、去构思看这几场。手建的章只说一句它不在构思里。 */
function ArrPlanStrip({ ch, snow, locked, onOpenPlan, onEditPlan }) {
  const planOwned = arrIsPlanChapter(ch);
  const act = ARR_ACTS.find((a) => a.id === ch.act) || ARR_ACTS[0];
  const range = arrRangeLabel(ch.structure);
  const first = (ch.scenes || []).find(arrIsPlanScene);
  const goal = ch.goal && ch.goal !== ch.summary ? ch.goal : "";
  if (!planOwned) {
    if (!snow || !snow.canPlan) return null;
    return (
      <div className="arr-planstrip is-desk" data-testid="arr-plan-strip">
        <span className="arr-planstrip-k"><I.Snowflake size={12} /> 构思</span>
        <span className="arr-planstrip-note">这一章是在这里手建的，不在构思的分章里——它的场、先后和名字都在这里改；重新「整理章节结构」时它原样留着。</span>
      </div>
    );
  }
  return (
    <div className="arr-planstrip" data-testid="arr-plan-strip">
      <div className="arr-planstrip-head">
        <span className="arr-planstrip-k"><I.Snowflake size={12} /> 构思里的这一章</span>
        <Tag tone={act.tone}>{act.n}</Tag>
        {ch.spine && <Tag tone="warn" title="这一章收在这个灾难上">{ch.spine}</Tag>}
        {range && <span className="arr-planstrip-range tab-num">{range}</span>}
        {ch.structure.titleAuto && <Tag tone="warn" outline title="章名还是系统起的占位——在上面直接改，或到「整理章节结构」里让 AI 起">还没起名</Tag>}
        <span className="arr-planstrip-actions">
          {snow && snow.pending ? (
            <button type="button" className="btn btn-quiet btn-xs" data-testid="arr-plan-resync" onClick={snow.onSync} disabled={locked || snow.busy}
              title={`构思有 ${snow.pending} 场改动还没同步到目录的场景卡`}>
              <I.Refresh size={12} /> {snow.busy ? "同步中…" : `同步 ${snow.pending} 场改动`}
            </button>
          ) : null}
          {first && (
            <button type="button" className="btn btn-quiet btn-xs" data-testid="arr-plan-scenes" onClick={() => onEditPlan(first)}
              title="去构思第 10 步，落在这一章的第一场上">
              <I.Snowflake size={12} /> 在构思里看这几场
            </button>
          )}
          <button type="button" className="btn btn-ghost btn-xs" data-testid="arr-plan-open" onClick={onOpenPlan}
            title="拆章 / 并章 / 挪章界 / AI 起章名——和构思页头的「整理章节结构」是同一张面板">
            <I.Layout size={12} /> 整理章节结构
          </button>
        </span>
      </div>
      {ch.summary && <p className="arr-planstrip-sum text-serif">{ch.summary}</p>}
      {goal && <p className="arr-planstrip-goal"><b>章目标</b>{goal}</p>}
    </div>
  );
}

/* ---- 编辑器 ---- */
function ArrEditor({
  ch, chapters, num, prev, next, numOf, sceneDnd, onAddScene, onCycleKind, onDeleteScene, onEditScene,
  onPatchTitle, onPatchDrama, onDeleteChapter, onOpenTrash, highlightSid, onClearHighlight, onJump, onBack, snow, chapterRun,
  sceneBatch, onOpenPlan, ctxOpen, onToggleCtx, warnCount, goView, onConfigureModel,
}) {
  const locked = ch.state === "approved";
  const refs = { scenes: useRef(null), handoff: useRef(null), drama: useRef(null), ai: useRef(null) };
  const anchors = [
    { key: "scenes", label: "场景" },
    { key: "handoff", label: "交接" },
    { key: "drama", label: "戏剧卡" },
    ...(ch.backendId ? [{ key: "ai", label: "AI 编排" }] : []),
  ];
  const jump = (key) => {
    const node = refs[key].current;
    if (node && node.scrollIntoView) node.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  /* 去处：构思第 10 步的那一场（与写作台、AI 起草台、成稿中心「回第 10 步」同一组意图）、写作台、AI 起草台 */
  const editInPlan = (s) => goView("snowflake", planIntentsForScene(s.backendId || s.sid));
  const forkWrite = (s) => goView("writer", writerIntents(s.sid));
  const forkAI = (s) => goView("scene", { type: "ws:scene-enqueue", detail: { sid: s.sid } });

  return (
    <section className="arr-ed" tabIndex={-1} aria-label={chapterLabel({ n: num }, { withTitle: false })}>
      <header className="arr-ed-head">
        <div className="arr-ed-head-l">
          <nav className="arr-ed-crumb" aria-label="位置">
            <button type="button" className="arr-back" onClick={onBack} title="返回全书编排"><I.Layers size={13} />全书编排</button>
            <span className="arr-crumb-sep" aria-hidden="true">/</span>
            <span className="tab-num">{chapterLabel({ n: num }, { withTitle: false })}</span>
          </nav>
          <input className="arr-ed-title text-serif arr-ed-title-input" defaultValue={ch.title} key={ch.id + "|" + ch.title}
            disabled={locked}
            title={arrIsPlanChapter(ch) ? "章名只有一个：在这里改，构思的章节表、场景列表的章头和分章面板跟着变" : undefined}
            onBlur={(e) => {
              const nextTitle = e.target.value.trim() || (arrIsPlanChapter(ch) ? `第 ${Number(num)} 章` : "未命名章节");
              if (nextTitle !== ch.title) onPatchTitle(nextTitle);
              else if (e.target.value !== ch.title) e.target.value = ch.title;
            }}
            onKeyDown={blurOnEnter} aria-label="章节标题" />
        </div>
        <div className="arr-ed-actions">
          <ArrChapterStateTag ch={ch} />
          <ArrChapterRunAction chapter={ch} {...chapterRun} />
          <IconButton icon="Trash" className="arr-del-ch" disabled={locked}
            label={locked ? "终稿已锁定，请先在成稿中心重新打开" : "删除本章"} onClick={onDeleteChapter} />
          <button type="button" className={`btn btn-ghost btn-sm arr-ctx-toggle ${ctxOpen ? "is-on" : ""}`}
            data-testid="arr-ctx-toggle" aria-expanded={ctxOpen} aria-controls="arr-ctx" onClick={onToggleCtx}>
            <I.ShieldCheck size={13} /> 体检{warnCount ? <span className="arr-ctx-count tab-num" aria-label={`${warnCount} 项待看`}>{warnCount}</span> : null}
          </button>
        </div>
        <div className="arr-ed-subbar">
          <nav className="arr-ed-anchors" aria-label="跳到本章的一部分">
            {anchors.map((a) => <button type="button" key={a.key} className="arr-anchor" onClick={() => jump(a.key)}>{a.label}</button>)}
          </nav>
          <span className="arr-auto-save" title="改动会立即写入目录，由后端版本收敛。"><I.Check size={12} /> 改动自动保存</span>
        </div>
      </header>

      <div className="arr-ed-body">
        {locked && (
          <Notice tone="info" icon={I.Lock}>本章是已批准终稿，章节结构与场景卡均为只读。需要修改请先到成稿中心「重新打开」。</Notice>
        )}
        <ArrPlanStrip ch={ch} snow={snow} locked={locked} onOpenPlan={onOpenPlan} onEditPlan={editInPlan} />

        {/* scene board —— 这一页真正干活的地方，放在最前面 */}
        <ArrSceneBoard ch={ch} chapters={chapters} locked={locked} sceneDnd={sceneDnd} sceneBatch={sceneBatch} sectionRef={refs.scenes}
          highlightSid={highlightSid} onClearHighlight={onClearHighlight} onAddScene={onAddScene} onCycleKind={onCycleKind}
          onDeleteScene={onDeleteScene} onEditScene={onEditScene} onOpenTrash={onOpenTrash}
          onEditPlan={editInPlan} onForkWrite={forkWrite} onForkAI={forkAI} />

        <ArrHandoffStrip prev={prev} ch={ch} next={next} numOf={numOf} onJump={onJump} sectionRef={refs.handoff} />
        <ArrDramaCard ch={ch} locked={locked} onPatchDrama={onPatchDrama} sectionRef={refs.drama} />
        {/* AI 编排：蓝图 / 方向 / 补全，咨询式补丁经作者逐条确认后原子回写目录 */}
        <ArrAiArrange ch={ch} locked={locked} sectionRef={refs.ai} onConfigureModel={onConfigureModel} onEditPlan={editInPlan} />
      </div>
    </section>
  );
}

export { ArrEditor };
