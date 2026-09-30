import React from "react";
import { I } from "./icons.jsx";
import { ARR_SCENE_STATE } from "./ws-author-data.js";
import { arrIsPlanScene, arrPovCandidates } from "./ws-author-derive.js";
import { ArrGrip, ArrSceneStateTag, blurOnEnter } from "./ws-author-ui.jsx";
import { KIND_FIELDS_GCS, KIND_FIELDS_RDD } from "./ws-catalog-adapt.js";
import { sceneDesignModel } from "./ws-scene-design.jsx";
import { useLibraryLive } from "./ws-library-store.js";
import { EmptyState, IconButton, Tag } from "./ws-ui.jsx";
import { sceneNoLabel } from "./labels/catalog.js";

/* ==========================================================
   章节详情 · 场景看板（从 ws-author-detail.jsx 拆出，2026-10）
   看板头（或多选时的批量条）· 本章场景进度 · 一场一行。
   阶段 Y「设计只有一处可改」：雪花整理出来、构思里那一行还在的场（design.owner === "plan"），
   形态 / 三拍 / POV 只在构思第 10 步改（ArrPlanBeats 是整句文字 + 直达那一场的链接）；手加的场三拍与 POV
   就在这里改（ArrDeskBeats）。题名、状态、删除、分流执行两种场都照常。
   ========================================================== */

const { useEffect, useMemo, useRef, useState } = React;

/* 视角候选：资料库的人物（资料库 store 的只读快照 useLibraryLive：挂上就按需读一次；以前读 window.LIB_ENTRIES，
   只有这次会话打开过资料库才有，冷启动进章节编排时下拉是空的）+ 目录里各场已经用过的视角名。快照不可变，
   资料库读回来 / 改了就换一份，候选跟着变。候选只是建议，仍可自由输入新名。 */
function useArrPovOptions(chapters) {
  const { entries } = useLibraryLive();
  return useMemo(() => arrPovCandidates(entries, chapters), [entries, chapters]);
}

const ARR_PLAN_OWNED_TIP = "这一场是雪花整理出来的：形态、三拍、视角在构思第 10 步改，确认后自动同步到这里";

/* 构思里定下的三拍：每拍最多两行，点一下展开全文；整句也在悬停提示里。
   多选态下整行只做「选择」：三拍不再是按钮，直达构思的链接也收起。 */
function ArrPlanBeats({ s, model, selectMode, onEditPlan }) {
  const [open, setOpen] = useState(false);
  const beats = model ? model.beats : [];
  const lines = (
    <>
      {beats.map((b) => (
        <span className="arr-beat" key={b.key} title={b.text || undefined}>
          <b>{b.label}</b><span className={`arr-beat-text ${b.text ? "" : "is-empty"}`}>{b.text || "未规划"}</span>
        </span>
      ))}
      <span className="arr-beat"><b>视角</b><span className={`arr-beat-text ${s.povName ? "" : "is-empty"}`}>{s.povName || "未规划"}</span></span>
    </>
  );
  return (
    <div className="arr-scene-brief is-plan-owned" data-testid="arr-scene-plan-owned">
      {selectMode ? <div className="arr-beats is-static">{lines}</div> : (
        <button type="button" className={`arr-beats ${open ? "is-open" : ""}`} aria-expanded={open}
          onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>
          {lines}
        </button>
      )}
      {!selectMode && (
        <button type="button" className="arr-plan-link" data-testid="arr-scene-edit-plan" title={ARR_PLAN_OWNED_TIP}
          onClick={(e) => { e.stopPropagation(); onEditPlan(s); }}><I.Snowflake size={11} /> 在构思里改</button>
      )}
    </div>
  );
}

/* 手加的场：三拍与 POV 就在这里改（失焦时只有真改了才写回）。三拍的叫法跟着形态走，与目录的 kindFields 同一套 */
function ArrDeskBeats({ s, onEdit, locked, povListId }) {
  const labels = s.kind === "反应" ? KIND_FIELDS_RDD : KIND_FIELDS_GCS;
  const bits = [[labels[0], "goal"], [labels[1], "obstacle"], [labels[2], "turn"]];
  const clean = (v) => (v === "—" ? "" : (v || ""));
  const commit = (key) => (e) => { const next = e.target.value.trim(); if (next !== clean(s[key]).trim()) onEdit({ [key]: next }); };
  return (
    <div className="arr-scene-brief">
      {bits.map(([label, key]) => (
        <label key={key} className="arr-gmc">
          <b>{label}</b>
          <input className="arr-gmc-input" defaultValue={clean(s[key])} key={s.sid + key + (s[key] || "")}
            placeholder="待定" onClick={(e) => e.stopPropagation()} disabled={locked} aria-label={`${s.title} · ${label}`}
            onBlur={commit(key)} onKeyDown={blurOnEnter} />
        </label>
      ))}
      <label className="arr-gmc arr-gmc-pov">
        <b>视角</b>
        <input className="arr-gmc-input" list={povListId} defaultValue={s.povName || ""} key={s.sid + "pov" + (s.povName || "")}
          placeholder="谁的视角" title="这一场的视角人物（按名字；新角色会自动建档）。起草前要定好视角。"
          onClick={(e) => e.stopPropagation()} disabled={locked} aria-label={`${s.title} · 视角`}
          onBlur={(e) => { const next = e.target.value.trim(); if (next !== (s.povName || "")) onEdit({ povName: next }); }}
          onKeyDown={blurOnEnter} />
      </label>
    </div>
  );
}

function ArrSceneRow({
  s, model, n, highlighted, onRowClick, onCycleKind, onDelete, onEdit, onMove, onEditPlan, onForkWrite, onForkAI,
  dragHandle, dropZone, locked = false, selectMode = false, selected = false, onToggleSelect, povListId,
}) {
  /* 雪花的场彼此的先后 = 构思第 9 步的行序：这里不给拖（手加的场照常能拖到任何两场之间）；形态也在构思里改 */
  const planOwned = arrIsPlanScene(s);
  const lockTip = "请先在成稿中心重新打开终稿";
  return (
    <li className={`arr-scene s-${s.state} ${highlighted ? "is-active" : ""} ${selected ? "is-selected" : ""} ${selectMode ? "is-selecting" : ""}`} {...dropZone}
      data-sid={s.sid}
      onClick={() => { if (selectMode) { if (!locked) onToggleSelect(s.sid); return; } onRowClick(); }}>
      {selectMode ? (
        <label className="arr-scene-check" onClick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={!!selected} disabled={locked} aria-label={`选择场景 ${s.title}`}
            onChange={() => onToggleSelect(s.sid)} />
        </label>
      ) : (
        <ArrGrip className="arr-scene-grip" movable={!planOwned && !locked} dnd={dragHandle} label={`移动场景「${s.title}」`} onMove={onMove}
          moveKey={"sc:" + s.sid}
          fixedTip={locked ? "终稿已锁定" : "雪花整理出来的场：先后在构思第 9 步「场景列表」里拖动，确认后自动同步到这里"} />
      )}
      <span className="arr-scene-num tab-num">{n}</span>
      {/* 多选态：整行只做「选择」——正文编辑、分流执行、单删一律收起，
          否则一个模式里同时摆着四种含义的点击，误操作只是时间问题 */}
      <span className="arr-scene-head" onClick={(e) => { if (!selectMode) e.stopPropagation(); }}>
        <input className="arr-scene-title-input text-serif" defaultValue={s.title} key={s.sid + s.title} title={s.title}
          disabled={locked || selectMode}
          onBlur={(e) => { const next = e.target.value.trim() || "未命名场景"; if (next !== s.title) onEdit({ title: next }); }}
          onKeyDown={blurOnEnter} aria-label="场景标题" />
      </span>
      <span className="arr-scene-acts" onClick={(e) => e.stopPropagation()}>
        {/* 分流执行：同一张场景卡，交给 AI 起草台排队，或自己去写作台写 */}
        {!selectMode && (
          <>
            <button type="button" className="btn btn-ghost btn-xs arr-scene-fork-ai" disabled={locked} title={locked ? lockTip : "把这张场景卡送进 AI 起草台排队"}
              onClick={(e) => { e.stopPropagation(); onForkAI(s); }}><I.Play size={11} /> 交给 AI</button>
            <button type="button" className="btn btn-ghost btn-xs arr-scene-fork-write" disabled={locked} title={locked ? lockTip : "带着这张卡去写作台写这一场"}
              onClick={(e) => { e.stopPropagation(); onForkWrite(s); }}><I.Pen size={11} /> 自己写</button>
          </>
        )}
        <button type="button" className="arr-pill-btn arr-cyc" disabled={locked || selectMode || planOwned}
          title={locked ? "终稿已锁定" : planOwned ? "形态（主动 / 反应）在构思第 10 步改" : "点击切换 主动 / 反应"}
          onClick={(e) => { e.stopPropagation(); if (!planOwned && onCycleKind) onCycleKind(); }}>
          <Tag tone={s.kind === "主动" ? "accent" : "info"} dot>{s.kind}</Tag>
        </button>
        <span className="arr-pill-readonly"><ArrSceneStateTag s={s} /></span>
        {!selectMode && (
          <IconButton icon="Trash" size="xs" className="arr-scene-more" disabled={locked}
            label={locked ? "终稿已锁定" : `把「${s.title}」移入回收站`} onClick={(e) => { e.stopPropagation(); if (onDelete) onDelete(); }} />
        )}
      </span>
      <span className="arr-scene-body" onClick={(e) => { if (!selectMode) e.stopPropagation(); }}>
        {planOwned
          ? <ArrPlanBeats s={s} model={model} selectMode={selectMode} onEditPlan={onEditPlan} />
          : <ArrDeskBeats s={s} onEdit={onEdit} locked={locked || selectMode} povListId={povListId} />}
      </span>
    </li>
  );
}

/* 场景看板：这一页真正干活的地方。与全书编排同一套模式语言：多选态下看板头部只剩批量操作。
   从结构镜头点进某一场时，把那一行滚进视野并短暂标出来（highlightSid）。 */
function ArrSceneBoard({
  ch, chapters, locked, sceneDnd, sceneBatch, highlightSid, onClearHighlight, onAddScene, onCycleKind, onDeleteScene, onEditScene,
  onOpenTrash, onEditPlan, onForkWrite, onForkAI, sectionRef,
}) {
  const scenes = ch.scenes || [];
  const tallies = { todo: 0, writing: 0, done: 0 };
  scenes.forEach((s) => { tallies[s.state] = (tallies[s.state] || 0) + 1; });
  const sb = sceneBatch;
  const sceneSelectMode = !!sb.mode && !locked;
  const sceneSelected = scenes.filter((s) => sb.has(s.sid)).length;
  const povOptions = useArrPovOptions(chapters);
  const povListId = povOptions.length ? `arr-pov-${ch.id}` : undefined;
  const models = useMemo(() => scenes.map((s, index) => (arrIsPlanScene(s) ? sceneDesignModel({ chapter: ch, scene: s, index }) : null)), [ch, scenes]);
  const listRef = useRef(null);

  useEffect(() => {
    if (!highlightSid || !listRef.current) return;
    const row = [...listRef.current.querySelectorAll(".arr-scene")].find((node) => node.getAttribute("data-sid") === highlightSid);
    if (row && row.scrollIntoView) row.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlightSid, ch.id]);

  return (
    <section className="card arr-scenes" ref={sectionRef} aria-labelledby={`arr-scenes-${ch.id}`}>
      <div className="card-head">
        {sceneSelectMode ? (
          <div className="arr-batch is-inline" role="toolbar" aria-label="场景批量操作">
            <span className="arr-batch-n">已选 <strong className="tab-num">{sceneSelected}</strong> / {scenes.length} 场</span>
            <span className="arr-batch-hint">点行选中；删除后进入回收站，可以恢复。</span>
            <button type="button" className="btn btn-quiet btn-sm" onClick={sb.onSelectAll}>
              {sceneSelected === scenes.length && scenes.length ? "取消全选" : "全选"}
            </button>
            <button type="button" className="btn btn-danger btn-sm" data-testid="author-batch-delete-scenes" disabled={!sceneSelected} onClick={sb.onDelete}>
              <I.Trash size={13} /> 删除所选{sceneSelected ? ` · ${sceneSelected} 场` : ""}
            </button>
            <button type="button" className="btn btn-ghost btn-sm" data-testid="author-scene-select-exit" onClick={sb.onToggleMode}>完成</button>
          </div>
        ) : (
          <>
            <div>
              <h2 className="card-title" id={`arr-scenes-${ch.id}`}>场景看板</h2>
              <div className="card-sub">{scenes.some(arrIsPlanScene)
                ? "雪花整理出来的场：设计与先后跟着构思走，换章用「整理章节结构」。手加的场可以拖到任何两场之间。"
                : "排场景顺序，把不要的场移入回收站。拖动抓手，或选中抓手用上下方向键挪。"}</div>
            </div>
            <div className="arr-scenes-actions">
              <button type="button" className="btn btn-quiet btn-sm" onClick={onOpenTrash} title="删掉的场景在回收站里，可以恢复">
                <I.Trash size={13} /> 回收站
              </button>
              <button type="button" className="btn btn-quiet btn-sm" data-testid="author-scene-select-mode" disabled={locked || !scenes.length}
                title={locked ? "终稿已锁定" : "多选场景后可以一次删除"} onClick={sb.onToggleMode}>
                <I.Check size={13} /> 多选
              </button>
              <button type="button" className="btn btn-accent btn-sm" disabled={locked} onClick={onAddScene}><I.Plus size={13} /> 新场景</button>
            </div>
          </>
        )}
      </div>

      <div className="arr-scene-tally" aria-label="本章场景进度">
        <span><i style={{ background: ARR_SCENE_STATE.done.dot }} />{ARR_SCENE_STATE.done.label} {tallies.done}</span>
        <span><i style={{ background: ARR_SCENE_STATE.writing.dot }} />{ARR_SCENE_STATE.writing.label} {tallies.writing}</span>
        <span><i style={{ background: ARR_SCENE_STATE.todo.dot }} />{ARR_SCENE_STATE.todo.label} {tallies.todo}</span>
      </div>

      {scenes.length ? (
        <ul className="arr-scene-list" ref={listRef}>
          {scenes.map((s, idx) => (
            <ArrSceneRow key={s.sid} s={s} model={models[idx]} n={sceneNoLabel(idx)} highlighted={highlightSid === s.sid}
              onRowClick={onClearHighlight} onCycleKind={() => onCycleKind(idx)}
              onDelete={() => onDeleteScene(idx)} onEdit={(patch) => onEditScene(idx, patch)} onMove={(dir) => sceneDnd.move(idx, dir)}
              onEditPlan={onEditPlan} onForkWrite={onForkWrite} onForkAI={onForkAI}
              dragHandle={sceneDnd.handle(idx)} dropZone={sceneDnd.zone(idx)} locked={locked}
              selectMode={sceneSelectMode} selected={sceneSelectMode && sb.has(s.sid)} onToggleSelect={sb.onToggle}
              povListId={povListId} />
          ))}
        </ul>
      ) : (
        <EmptyState compact icon="Layers" title="这一章还没有场">用「新场景」加第一场；删掉的场在回收站里。</EmptyState>
      )}
      {povListId ? <datalist id={povListId}>{povOptions.map((name) => <option key={name} value={name} />)}</datalist> : null}
    </section>
  );
}

export { ArrSceneBoard, ArrSceneRow };
