import React from "react";
import { I } from "./icons.jsx";
import { ARR_ACTS, ARR_SCENE_STATE } from "./ws-author-data.jsx";
import { arrChapterFacts, arrIsPlanChapter, arrIsPlanScene, arrRangeLabel, DRAMA_KEYS } from "./ws-author-derive.js";
import { ArrAiArrange } from "./ws-author-ai.jsx";
import { ArrChapterStateTag, ArrGrip, ArrSceneStateTag } from "./ws-author-ui.jsx";
import { ArrChapterRunAction } from "./ws-chapter-run.jsx";
import { planIntentsForScene, sceneDesignModel } from "./ws-scene-design.jsx";
import { EmptyState, IconButton, Notice, Tag } from "./ws-ui.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { chapterLabel, sceneNoLabel } from "./ws-labels.js";

/* ==========================================================
   章节详情 — 中间的编辑器
   页头（面包屑 · 章名 · 状态 / 运行 / 删除 / 体检 · 锚点）→ 构思条 → 场景看板 → 交接 → 戏剧卡 → AI 编排。
   跨视图的去处（写作台、AI 起草台、构思第 10 步）都走外壳给的 goView。
   ========================================================== */

const { useState, useRef, useEffect, useMemo } = React;

/* 行内输入框按回车 = 改完了（失焦写回）。输入法确认候选的那一下回车不算：拼音选词时按回车会把半截题名写进目录、把作者甩出输入框 */
const blurOnEnter = (e) => { if (e.key === "Enter" && !isImeComposing(e)) e.target.blur(); };

/* POV 候选（best-effort，取自资料库人物；冷启动没有人物时为空，仍可自由输入新名） */
const arrPovOptions = () => {
  try { return [...new Set((window.LIB_ENTRIES || []).filter((e) => e.cat === "people").map((e) => e.name).filter(Boolean))]; }
  catch (e) { return []; }
};

/* ---- 戏剧卡 ---- */
const ARR_DRAMA_GROUPS = [
  { key: "promise", label: "承诺", icon: "Star", fields: [
    { k: "promise", label: "核心承诺", hint: "读完这一章读者会得到什么", primary: true },
    { k: "problem", label: "章节问题", hint: "本章想问读者一个什么问题" },
  ] },
  { key: "drive", label: "推进", icon: "ArrowRight", fields: [
    { k: "spine", label: "主线推进", hint: "本章在全书主线上前进了多少" },
    { k: "arc", label: "人物变化", hint: "主要人物的内在或外在变化" },
  ] },
  { key: "close", label: "收束", icon: "Sparkles", fields: [
    { k: "aftertaste", label: "结尾余味", hint: "读完最后一段的感觉" },
    { k: "ending", label: "结尾效果", hint: "最后一句具体的画面 / 动作" },
  ] },
];

/* 戏剧卡的一格。textarea 不受控（边写边存会打断输入法），所以 key 带上服务端的值：
   AI 编排写入、目录重拉带来新值时换一个新的框显示新值；失焦时只有真的改了才写回——
   以前 key 只有章 id，重拉之后框里还是旧字，下一次失焦把旧字写回去，把刚应用的建议悄悄冲掉。 */
function ArrDramaText({ className, label, value, ck, locked, onCommit, placeholder }) {
  const v = value || "";
  return (
    <textarea className={className} defaultValue={v} key={`${ck}|${v}`} placeholder={placeholder} disabled={locked}
      aria-label={label}
      onBlur={(e) => { if (e.target.value !== v && onCommit) onCommit(e.target.value); }} />
  );
}

function ArrDramaCard({ ch, locked, onPatchDrama, sectionRef }) {
  const drama = ch.drama || {};
  const filled = DRAMA_KEYS.filter((k) => String(drama[k] || "").trim()).length;
  const guarded = !!(String(drama.forbidden || "").trim() || String(drama.notes || "").trim());
  /* null = 跟着内容走（一格都没写就收成一行）；作者点过就听作者的。偏好记着是哪一章的，换章即回到「跟着内容走」。
     自动展开只往「开」的方向走：开过一次就钉住——清空唯一写过的一格、Tab 到下一格时，失焦写回让内容变空，
     卡片要是跟着收起，刚拿到焦点的那一格就被卸掉，焦点掉到 body 上。 */
  const autoOpen = filled > 0 || guarded;
  const [pref, setPref] = useState(() => ({ id: ch.id, open: null }));
  const openPref = pref.id === ch.id ? pref.open : null;
  useEffect(() => {
    if (openPref == null && autoOpen) setPref({ id: ch.id, open: true });
  }, [ch.id, openPref, autoOpen]);
  const open = openPref == null ? autoOpen : openPref;
  const setOpenPref = (value) => setPref({ id: ch.id, open: value });
  const bodyId = `arr-drama-${ch.id}`;
  return (
    <section className={`card arr-drama ${open ? "is-open" : "is-collapsed"}`} ref={sectionRef} aria-labelledby={`${bodyId}-title`}>
      <button type="button" className="arr-drama-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpenPref(!open)}>
        <span className="arr-drama-toggle-main">
          <span className="card-title" id={`${bodyId}-title`}>戏剧卡</span>
          <Tag tone="neutral" className="tab-num">可选 · {filled}/{DRAMA_KEYS.length}</Tag>
          <I.ChevronDown size={15} className="arr-drama-chev" />
        </span>
        <span className="card-sub">这一章的写法：承诺、推进和余味。它归这里（不在构思里），会进章节蓝图和本章每一场的 AI 起草上下文；留空也能写。</span>
      </button>
      {open && (
        <div className="arr-drama-groups" id={bodyId}>
          {ARR_DRAMA_GROUPS.map((g) => {
            const Ic = I[g.icon] || I.Dot;
            return (
              <div className="arr-dgroup" key={g.key}>
                <header className="arr-dgroup-head"><Ic size={13} /><span>{g.label}</span><i className="arr-dgroup-rule" /></header>
                <div className="arr-dgroup-fields">
                  {g.fields.map((f) => (
                    <div className={`arr-field ${f.primary ? "is-primary" : ""}`} key={f.k}>
                      <header className="arr-field-head">
                        <span className="arr-field-label">{f.label}</span>
                        <span className="arr-field-hint">{f.hint}</span>
                      </header>
                      <ArrDramaText className="arr-field-text" label={f.label} value={drama[f.k]} ck={`${ch.id}|${f.k}`}
                        locked={locked} placeholder="还没写" onCommit={(v) => onPatchDrama(f.k, v)} />
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
          <div className="arr-dgroup arr-dgroup-guard">
            <header className="arr-dgroup-head"><I.ShieldCheck size={13} /><span>护栏</span><i className="arr-dgroup-rule" /></header>
            <div className="arr-guard-grid">
              <div className="arr-guard" data-tone="danger">
                <div className="arr-guard-label"><I.Ban size={12} /> 禁止包含</div>
                <ArrDramaText className="arr-guard-text" label="禁止包含" value={drama.forbidden} ck={`${ch.id}|forbidden`} locked={locked}
                  placeholder="这一章不能出现的词或情节，一行一条" onCommit={(v) => onPatchDrama("forbidden", v)} />
              </div>
              <div className="arr-guard" data-tone="info">
                <div className="arr-guard-label"><I.Quote size={12} /> 备注</div>
                <ArrDramaText className="arr-guard-text" label="章节备注" value={drama.notes} ck={`${ch.id}|notes`} locked={locked}
                  placeholder="给自己留的话" onCommit={(v) => onPatchDrama("notes", v)} />
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

/* ---- 场景看板的一行 ----
   阶段 Y「设计只有一处可改」：雪花整理出来、构思里那一行还在的场（design.owner === "plan"），
   形态 / 三拍 / POV 只在构思第 10 步改，确认后自动同步回来。台子上照样能改的话，两边从此各说各话——
   所以这里只读（整句文字，不是只读输入框），给一条直达那一场的链接。题名、状态、删除、分流执行照常。 */
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

/* 手加的场：三拍与 POV 就在这里改（失焦时只有真改了才写回） */
function ArrDeskBeats({ s, onEdit, locked, povListId }) {
  const reactive = s.kind === "反应";
  const bits = reactive
    ? [["反应", "goal"], ["两难", "obstacle"], ["决定", "turn"]]
    : [["目标", "goal"], ["冲突", "obstacle"], ["挫败", "turn"]];
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

/* ---- 章与章的交接 ----
   入口 / 出口作者填过就用作者的；没填过（产品里没有编辑入口，真实作品全是空的）就从场上读：
   入口 = 第一场在做什么，出口 = 最后一场离场时变了什么——两章之间接不接得上，一眼看得出来。 */
function ArrHandoffStrip({ prev, ch, next, numOf, onJump, sectionRef }) {
  const mine = arrChapterFacts(ch);
  const prevExit = prev ? arrChapterFacts(prev).exit : null;
  const nextEntry = next ? arrChapterFacts(next).entry : null;
  const derived = mine.entry.derived || mine.exit.derived;
  return (
    <div className="arr-handoff" data-testid="arr-handoff" ref={sectionRef}>
      <button type="button" className={`arr-ho-cell arr-ho-side ${prev ? "" : "is-empty"}`} disabled={!prev} onClick={() => prev && onJump(prev.id)}>
        <span className="arr-ho-k"><I.ChevronLeft size={12} />{prev ? `承接第 ${Number(numOf[prev.id])} 章` : "全书开篇"}</span>
        <span className="arr-ho-text" title={prev ? prevExit.text : undefined}>{prev ? (prevExit.text || "上一章还没有出口") : "没有前一章"}</span>
      </button>
      <div className="arr-ho-cell arr-ho-mid">
        <span className="arr-ho-k">本章的入口和出口{derived ? <em className="arr-ho-src" title="章级的入口 / 出口没有单独填过：入口取第一场，出口取最后一场的离场变化">取自首尾两场</em> : null}</span>
        <span className="arr-ho-text arr-ho-entry" title={mine.entry.text}><i className="arr-ho-tick">入</i><span className="arr-ho-clamp">{mine.entry.text || "还没有场"}</span></span>
        <span className="arr-ho-text arr-ho-exit" title={mine.exit.text}><i className="arr-ho-tick is-out">出</i><span className="arr-ho-clamp">{mine.exit.text || "还没有场"}</span></span>
      </div>
      <button type="button" className={`arr-ho-cell arr-ho-side ${next ? "" : "is-empty"}`} disabled={!next} onClick={() => next && onJump(next.id)}>
        <span className="arr-ho-k">{next ? `交给第 ${Number(numOf[next.id])} 章` : "全书收束"}<I.ChevronRight size={12} /></span>
        <span className="arr-ho-text" title={next ? nextEntry.text : undefined}>{next ? (nextEntry.text || "下一章还没有场") : "没有后一章"}</span>
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
  ch, num, prev, next, numOf, sceneDnd, onAddScene, onCycleKind, onDeleteScene, onEditScene,
  onPatchTitle, onPatchDrama, onDeleteChapter, onOpenTrash, highlightSid, onClearHighlight, onJump, onBack, snow, chapterRun,
  sceneBatch, onOpenPlan, ctxOpen, onToggleCtx, warnCount, goView, onConfigureModel,
}) {
  const scenes = ch.scenes || [];
  const tallies = { todo: 0, writing: 0, done: 0 };
  scenes.forEach((s) => { tallies[s.state] = (tallies[s.state] || 0) + 1; });
  const locked = ch.state === "approved";
  const sb = sceneBatch;
  const sceneSelectMode = !!sb.mode && !locked;
  const sceneSelected = scenes.filter((s) => sb.has(s.sid)).length;
  const povOptions = useMemo(arrPovOptions, [ch.id]);
  const povListId = povOptions.length ? `arr-pov-${ch.id}` : undefined;
  const models = useMemo(() => scenes.map((s, index) => (arrIsPlanScene(s) ? sceneDesignModel({ chapter: ch, scene: s, index }) : null)), [ch, scenes]);
  const refs = { scenes: useRef(null), handoff: useRef(null), drama: useRef(null), ai: useRef(null) };
  const listRef = useRef(null);
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
  const forkWrite = (s) => goView("writer", { type: "ws:writer-scene", detail: s.sid });
  const forkAI = (s) => goView("scene", { type: "ws:scene-enqueue", detail: { sid: s.sid } });

  /* 从结构镜头点进某一场：把那一行滚进视野并短暂标出来（没有场景面板，常亮的高亮只会让第一场显得特别） */
  useEffect(() => {
    if (!highlightSid || !listRef.current) return;
    const row = [...listRef.current.querySelectorAll(".arr-scene")].find((node) => node.getAttribute("data-sid") === highlightSid);
    if (row && row.scrollIntoView) row.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlightSid, ch.id]);

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
        <section className="card arr-scenes" ref={refs.scenes} aria-labelledby={`arr-scenes-${ch.id}`}>
          {/* 与全书编排同一套模式语言：多选态下看板头部只剩批量操作 */}
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
                  onEditPlan={editInPlan} onForkWrite={forkWrite} onForkAI={forkAI}
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

        <ArrHandoffStrip prev={prev} ch={ch} next={next} numOf={numOf} onJump={onJump} sectionRef={refs.handoff} />
        <ArrDramaCard ch={ch} locked={locked} onPatchDrama={onPatchDrama} sectionRef={refs.drama} />
        {/* AI 编排：蓝图 / 方向 / 补全，咨询式补丁经作者逐条确认后原子回写目录 */}
        <ArrAiArrange ch={ch} locked={locked} sectionRef={refs.ai} onConfigureModel={onConfigureModel} />
      </div>
    </section>
  );
}

export { ArrEditor };
