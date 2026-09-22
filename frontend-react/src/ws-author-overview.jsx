import React from "react";
import { I } from "./icons.jsx";
import { ARR_ACTS, ARR_SCENE_STATE } from "./ws-author-data.jsx";
import { arrBookFacts, arrChapterStatus, arrIsPlanChapter, arrLensChapters, arrRangeLabel } from "./ws-author-derive.js";
import { ArrPacingLens } from "./ws-author-pacing.jsx";
import { ArrSpineLens } from "./ws-author-spine.jsx";
import { ArrDoctor } from "./ws-author-doctor.jsx";
import { ArrBudgetBar, ArrChapterStateTag, ArrGrip, ArrMiniScenes } from "./ws-author-ui.jsx";
import { PageHeader, Segmented, Tag } from "./ws-ui.jsx";
import { chapterHeading, chapterLabel } from "./ws-labels.js";

/* ==========================================================
   全书编排 — overview
   页头（或多选时的批量条）· 结构 / 节奏两个镜头 · 全书体检 · 按卷分组的章节清单（管理用：镜头负责「看」，清单负责「动」）
   ========================================================== */

const ARR_PLAN_CHAPTER_TIP = "这一章是构思里分出来的：它排第几、在第几卷、装哪几场，由「整理章节结构」决定";

/* 页头。多选是一个独占模式：进入后头部只剩这条批量操作栏，
   页面上不再同时摆着「新建 / 切视图 / 逐章打开」等与选择无关的动作 */
function ArrOverviewHead({ batch, snow, onOpenPlan, onRefresh, onMode, onNew, newTip }) {
  return (
    <div className={`arr-ov-head ${batch.mode ? "is-selecting" : ""}`}>
      {batch.mode ? (
        <div className="arr-batch" role="toolbar" aria-label="章节批量操作">
          <span className="arr-batch-n">已选 <strong className="tab-num">{batch.selectedCount}</strong> / {batch.selectableCount} 章</span>
          <span className="arr-batch-hint">点行选中；已批准终稿不可选。删除后进入回收站，可以恢复。</span>
          <button type="button" className="btn btn-quiet btn-sm" onClick={batch.onSelectAll} disabled={!batch.selectableCount}>
            {batch.allSelected ? "取消全选" : "全选"}
          </button>
          <button type="button" className="btn btn-danger btn-sm" data-testid="author-batch-delete-chapters"
            disabled={!batch.selectedCount} onClick={batch.onDelete}>
            <I.Trash size={13} /> 删除所选{batch.selectedCount ? ` · ${batch.selectedCount} 章` : ""}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" data-testid="author-chapter-select-exit" onClick={batch.onExit}>完成</button>
        </div>
      ) : (
        <PageHeader className="arr-ov-pagehead" title="章节编排"
          actions={(
            <>
              {snow.canPlan && (
                <button type="button" className="btn btn-ghost" data-testid="author-open-plan" onClick={onOpenPlan}
                  title="拆章 / 并章 / 挪章界 / AI 起章名——和构思页头的「整理章节结构」是同一张面板，确认写入后目录跟着变">
                  <I.Layout size={14} /> 整理章节结构{snow.pending ? ` · ${snow.pending} 场待同步` : ""}
                </button>
              )}
              <button type="button" className="btn btn-quiet" onClick={onRefresh} title="从服务端重新载入目录"><I.Refresh size={14} /> 刷新</button>
              <Segmented value="overview" onChange={onMode} label="章节编排视图"
                options={[{ value: "overview", label: "全书编排" }, { value: "detail", label: "章节详情" }]} />
              <button type="button" className="btn btn-quiet" data-testid="author-chapter-select-mode"
                title="多选章节后可以一次删除（已批准终稿不可选）"
                disabled={!batch.selectableCount} onClick={batch.onEnter}>
                <I.Check size={14} /> 多选
              </button>
              <button type="button" className="btn btn-accent" onClick={onNew} title={newTip}>
                <I.Plus size={14} /> 新建章节
              </button>
            </>
          )} />
      )}
    </div>
  );
}

/* 章节清单的一行。整行可点开；键盘走标题按钮。 */
function ArrChapterRow({ c, num, picked, onOpen, dnd, selectMode, selected, onToggleSelect, movable, onMove }) {
  const scenes = c.scenes || [];
  const done = scenes.filter((s) => s.state === "done").length;
  const locked = c.state === "approved";
  const status = arrChapterStatus(c);
  /* 阶段 X：雪花整理出来的章没有「章承诺」和章级 POV——行上不留空，用构思里的章摘要和各场的 POV 顶上 */
  const blurb = c.promise || c.summary || "";
  const povs = c.pov ? [c.pov] : [...new Set(scenes.map((s) => s.povName).filter(Boolean))];
  const povLine = povs.length > 2 ? `${povs.slice(0, 2).join("、")} 等 ${povs.length} 人` : povs.join("、");
  const planOwned = arrIsPlanChapter(c);
  const rangeLabel = arrRangeLabel(c.structure);
  const words = (c.words && c.words.cur) || 0;
  const target = (c.words && c.words.target) || 0;
  // 章号「第 N 章」只在有真章名时作名字前的小字；占位名（第 N 章 / 未命名）只写一遍
  const head = chapterHeading({ n: num, title: c.title });
  const full = chapterLabel({ n: num, title: c.title }, { maxTitle: Infinity });
  return (
    <li className={`arr-card s-${status.key} ${picked ? "is-picked" : ""} ${selected ? "is-selected" : ""} ${selectMode && locked ? "is-unselectable" : ""} ${planOwned ? "is-plan-owned" : ""}`}
      data-testid="arr-chapter-card" data-structure-owner={planOwned ? "plan" : "desk"}
      {...(selectMode ? {} : dnd)}
      onClick={() => { if (!selectMode) { onOpen(c.id); return; } if (!locked) onToggleSelect(c.id); }}>
      {selectMode ? (
        <label className="arr-card-check" onClick={(e) => e.stopPropagation()}
          title={locked ? "已批准终稿不可删除——请先到成稿中心重新打开" : "选中本章"}>
          <input type="checkbox" checked={!!selected} disabled={locked}
            aria-label={`选择${full}`}
            onChange={() => onToggleSelect(c.id)} />
        </label>
      ) : (
        <ArrGrip className="arr-card-grip" movable={movable} label={`移动${head.num}`} onMove={onMove} moveKey={"ch:" + c.id}
          fixedTip={locked ? "已批准终稿的位置锁定" : planOwned ? ARR_PLAN_CHAPTER_TIP : ""} />
      )}
      <span className="arr-card-main">
        <span className="arr-card-line">
          {head.title && <span className="arr-card-chno tab-num">{head.num}</span>}
          {selectMode
            ? <span className="arr-card-title text-serif">{head.title || head.num}</span>
            : <button type="button" className="arr-card-title text-serif" title={full} onClick={(e) => { e.stopPropagation(); onOpen(c.id); }}>{head.title || head.num}</button>}
        </span>
        {blurb ? <span className="arr-card-promise" title={blurb}>{blurb}</span> : null}
      </span>
      <span className="arr-card-range tab-num" title={rangeLabel ? "这一章装着构思「场景列表」里的这几场" : undefined}>{rangeLabel}</span>
      <span className="arr-card-spine">{c.spine ? <Tag tone="warn" title="这一章收在这个灾难上（来自构思的分章）">{c.spine}</Tag> : null}</span>
      <span className="arr-card-pov" title={povs.join("、")}>{povLine ? <><I.Eye size={12} /><span>{povLine}</span></> : null}</span>
      <span className="arr-card-scenes" title={`${scenes.length} 场，${ARR_SCENE_STATE.done.label} ${done} 场`}><ArrMiniScenes scenes={scenes} /><span className="tab-num">{done}/{scenes.length}</span></span>
      <span className="arr-card-words">
        <span className="tab-num">{words.toLocaleString()}{target > 0 ? ` / ${target.toLocaleString()}` : ""} 字</span>
        <ArrBudgetBar cur={words} target={target} />
      </span>
      <span className="arr-card-state"><ArrChapterStateTag ch={c} /></span>
    </li>
  );
}

/* 全书编排的镜头：结构 / 节奏都读真实数据。旧版记住的「arc / loom」镜头已经没有了，落回结构。 */
const ARR_LENSES = [
  { key: "spine", label: "结构", title: "全书结构", sub: "卷、章、场按故事序排开。三个灾难各自收束一章；点章进详情，点场落在那一场上。" },
  { key: "pace", label: "节奏镜头", title: "节奏镜头", sub: "按章的字数（虚框是目标）和各场的视角。看哪一章太长、哪一章太薄、视角换得勤不勤。" },
];

function ArrOverview({ chapters, numOf, pickedId, onOpen, onOpenScene, rowDnd, boardDnd, onNew, lens, setLens, batch, snow, onOpenPlan, canMove, onMoveChapter }) {
  const selectMode = !!batch.mode;
  const book = React.useMemo(() => arrBookFacts(chapters), [chapters]);
  const lensChapters = React.useMemo(() => arrLensChapters(chapters), [chapters]);
  const activeLens = ARR_LENSES.find((l) => l.key === lens) || ARR_LENSES[0];

  return (
    <div className="arr-ov-scroll">
      <section className="card arr-arc" aria-labelledby="arr-lens-title">
        <div className="card-head">
          <div>
            <h2 className="card-title" id="arr-lens-title">{activeLens.title}</h2>
            <div className="card-sub">{activeLens.sub}</div>
          </div>
          <Segmented value={activeLens.key} onChange={setLens} label="全书镜头" size="sm"
            options={ARR_LENSES.map((l) => ({ value: l.key, label: l.label }))} />
        </div>
        {activeLens.key === "pace"
          ? <ArrPacingLens chapters={lensChapters} numOf={numOf} onOpen={onOpen} />
          : <ArrSpineLens chapters={chapters} numOf={numOf} pickedId={pickedId} onOpen={onOpen} onOpenScene={onOpenScene} />}
      </section>

      <ArrDoctor chapters={chapters} numOf={numOf} onOpen={onOpen} onLens={setLens} book={book} snow={snow} onOpenPlan={onOpenPlan} />

      <section className="card arr-board-card" aria-labelledby="arr-board-title">
        <div className="card-head">
          <div>
            <h2 className="card-title" id="arr-board-title">章节</h2>
            <div className="card-sub">
              {book.planned
                ? "按卷排列。构思分出来的章先后在「整理章节结构」里改；手建的章可以拖动，或选中抓手用上下方向键挪。"
                : "按卷排列。拖动抓手重排，或选中抓手用上下方向键挪；拖到别的卷就归那一卷。"}
            </div>
          </div>
        </div>
        {ARR_ACTS.map((a) => {
          const items = chapters.filter((c) => c.act === a.id);
          const w = items.reduce((s, c) => s + ((c.words && c.words.cur) || 0), 0);
          return (
            <section className="arr-actsec" key={a.id} aria-label={a.n}>
              <header className="arr-actsec-head">
                <Tag tone={a.tone}>{a.n}</Tag>
                <span className="arr-actsec-meta tab-num">{items.length} 章 · {w.toLocaleString()} 字</span>
              </header>
              <ol className="arr-board" {...boardDnd(a.id)}>
                {/* 多选态不再叠加「当前章」高亮：两者都是 crimson 描边，同屏出现分不清哪张是选中的 */}
                {items.map((c) => (
                  <ArrChapterRow key={c.id} c={c} num={numOf[c.id]} picked={!selectMode && c.id === pickedId} onOpen={onOpen} dnd={rowDnd(c.id)}
                    selectMode={selectMode} selected={selectMode && batch.has(c.id)} onToggleSelect={batch.onToggle}
                    movable={canMove(c)} onMove={(dir) => onMoveChapter(c.id, dir)} />
                ))}
                {!items.length && selectMode ? <li className="arr-board-empty">这一卷还没有章</li> : null}
              </ol>
              {!selectMode && (
                <button type="button" className="arr-card-add" onClick={() => onNew(a.id)}>
                  <I.Plus size={14} /><span>在{a.n}新建章节</span>
                </button>
              )}
            </section>
          );
        })}
      </section>
    </div>
  );
}

export { ArrOverview, ArrOverviewHead };
