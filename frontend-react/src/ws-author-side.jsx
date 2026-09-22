import React from "react";
import { I } from "./icons.jsx";
import { ARR_ACTS, ARR_CH_STATE } from "./ws-author-data.jsx";
import { arrChapterFacts, arrChapterStatus, arrIsPlanChapter, arrRangeLabel } from "./ws-author-derive.js";
import { ArrAiHealth } from "./ws-author-ai.jsx";
import { ArrChapterStateTag, ArrMiniScenes } from "./ws-author-ui.jsx";
import { isImeComposing, useFocusTrap } from "./ws-dialog.jsx";
import { CloseButton } from "./ws-ui.jsx";
import { chapterHeading, chapterLabel } from "./ws-labels.js";

/* ==========================================================
   章节详情的两侧
   · ArrRail —— 左边的章节序列（≤1180 收成一列章号）
   · ArrChapterContext —— 右边的章节体检 + 视角 · 时空（≤1360 变成从页头「体检」按钮拉出的抽屉；
     jsdom 里没有媒体查询，它一直在 DOM 里）
   ========================================================== */

function ArrCheckRow({ ok, warn, label, val }) {
  const Ic = ok ? I.Check : warn ? I.AlertTriangle : I.Circle;
  return (
    <li className={`arr-check ${ok ? "is-ok" : warn ? "is-warn" : ""}`}>
      <Ic size={13} />
      <span className="arr-check-label">{label}</span>
      <span className="arr-check-val tab-num">{val}</span>
    </li>
  );
}

function ArrChapterContext({ ch, checks, snow, onOpenPlan, open, onClose, onConfigureModel }) {
  const facts = arrChapterFacts(ch);
  const asideRef = React.useRef(null);
  /* 只有抽屉形态（≤1360，页头「体检」按钮拉出来）才会 open：焦点进抽屉、Tab 不逃出去、关上后回到「体检」按钮。
     抽屉开着时它就是模态对话框（role=dialog + aria-modal），读屏用户知道自己被关在这一块里；
     宽屏第三栏形态只是一块有名字的区域。 */
  useFocusTrap(asideRef, open);
  return (
    <aside id="arr-ctx" ref={asideRef} tabIndex={-1} className={`arr-ctx ${open ? "is-open" : ""}`}
      {...(open
        ? { role: "dialog", "aria-modal": "true", "aria-labelledby": "arr-ctx-title" }
        : { "aria-label": "章节体检与视角时空" })}
      onKeyDown={(e) => { if (e.key === "Escape" && open && !isImeComposing(e)) { e.stopPropagation(); onClose(); } }}>
      <div className="arr-ctx-drawerhead">
        <span id="arr-ctx-title">本章体检</span>
        <CloseButton label="收起体检" onClick={onClose} className="arr-ctx-close" />
      </div>
      <div className="ctx-block">
        <div className="ctx-head"><I.ShieldCheck size={13} /><span>章节体检</span></div>
        {/* 只报读得出来的事实（ws-author-derive.js 的 arrChapterChecks）。「与上一章出口对齐」「线索待交接」
            读的章级字段没有编辑入口，已经不报了。 */}
        <ul className="arr-checks">
          {checks.map((row) => <ArrCheckRow key={row.key} ok={row.ok} warn={row.warn} label={row.label} val={row.val} />)}
        </ul>
        {/* AI 体检与上面的规则体检合在一处：规则免费兜底，AI 补结构性判断 */}
        <ArrAiHealth ch={ch} locked={ch.state === "approved"} onConfigureModel={onConfigureModel} />
        {snow && snow.canPlan ? (
          <button type="button" className="btn btn-quiet btn-sm arr-ctx-plan" data-testid="arr-ctx-open-plan" onClick={onOpenPlan}>
            <I.Layout size={13} /> 整理章节结构
          </button>
        ) : null}
      </div>

      <div className="ctx-block" data-testid="arr-ctx-spacetime">
        <div className="ctx-head"><I.Eye size={13} /><span>视角 · 时空</span>
          {(facts.pov.derived || facts.time.derived || facts.place.derived) ? <span className="arr-ctx-hint">取自本章各场</span> : null}
        </div>
        <ul className="arr-meta">
          <li><span>视角</span><strong>{facts.pov.text || "—"}</strong></li>
          <li><span>时间</span><strong>{facts.time.text || "—"}</strong></li>
          <li><span>地点</span><strong>{facts.place.text || "—"}</strong></li>
        </ul>
      </div>
    </aside>
  );
}

/* 序列栏的一行是整行按钮（点开这一章），里面放不下第二个按钮当抓手：
   手建的章在行上按 Alt + 上下方向键挪（和全书清单抓手上的方向键是同一个 move）。 */
function ArrRail({ chapters, numOf, pickedId, onPick, rowDnd, boardDnd, onBack, onNew, canMove, onMove }) {
  const counts = { approved: 0, going: 0, planned: 0 };
  chapters.forEach((c) => {
    const key = arrChapterStatus(c).key;
    if (key === "approved") counts.approved += 1;
    else if (key === "planned") counts.planned += 1;
    else counts.going += 1;
  });

  return (
    <aside className="arr-rail" aria-label="章节序列">
      <header className="arr-rail-head">
        <button type="button" className="arr-back" onClick={onBack}><I.Layers size={13} /><span>全书编排</span></button>
        <h2 className="arr-rail-title text-serif">章节序列</h2>
      </header>
      <div className="arr-rail-stat">
        <span><strong className="tab-num">{counts.approved}</strong> {ARR_CH_STATE.approved.label}</span>
        <span><strong className="tab-num">{counts.going}</strong> {ARR_CH_STATE.writing.label}</span>
        <span><strong className="tab-num">{counts.planned}</strong> {ARR_CH_STATE.planned.label}</span>
      </div>
      <div className="arr-rail-list">
        {ARR_ACTS.map((a) => {
          const items = chapters.filter((c) => c.act === a.id);
          return (
            <div className="arr-rail-act" key={a.id} {...boardDnd(a.id)}>
              <div className="arr-rail-acthead" data-tone={a.tone}>{a.n}</div>
              <ul>
                {items.map((c) => {
                  const done = (c.scenes || []).filter((s) => s.state === "done").length;
                  const planOwned = arrIsPlanChapter(c);
                  const movable = canMove(c);
                  // 章号「第 N 章」：有真章名时进第二行的小字；占位名（第 N 章 / 未命名）只在名字位置写一遍
                  const head = chapterHeading({ n: numOf[c.id], title: c.title });
                  const full = chapterLabel({ n: numOf[c.id], title: c.title }, { maxTitle: Infinity });
                  const tip = planOwned
                    ? `${full}：${arrRangeLabel(c.structure) || "构思分出来的章"}，先后在「整理章节结构」里改`
                    : movable ? `${full}：拖动，或按 Alt + 上下方向键挪` : full;
                  return (
                    <li key={c.id}>
                      <button type="button" className={`arr-rail-row ${c.id === pickedId ? "is-active" : ""}`} {...rowDnd(c.id)} onClick={() => onPick(c.id)}
                        aria-current={c.id === pickedId ? "true" : undefined}
                        aria-label={full}
                        aria-keyshortcuts={movable ? "Alt+ArrowUp Alt+ArrowDown" : undefined}
                        data-arr-move={"ch:" + c.id}
                        title={tip}
                        onKeyDown={(e) => {
                          if (!movable || !e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
                          e.preventDefault();
                          onMove(c.id, e.key === "ArrowUp" ? -1 : 1);
                        }}>
                        <span className={`arr-rail-grip ${movable ? "" : "is-fixed"}`} aria-hidden="true"><I.GripVertical size={13} /></span>
                        {/* 只在收窄成一列章号时出现（≤1180）：紧凑的「1」，完整叫法在按钮的无障碍名与提示里 */}
                        <span className="arr-rail-num tab-num" aria-hidden="true">{Number(numOf[c.id]) || numOf[c.id]}</span>
                        <span className="arr-rail-body">
                          <span className="arr-rail-name text-serif">{head.title || head.num}</span>
                          <span className="arr-rail-meta">
                            {head.title && <span className="arr-rail-chno tab-num">{head.num}</span>}
                            <ArrMiniScenes scenes={c.scenes || []} /><span className="tab-num">{done}/{(c.scenes || []).length}</span>
                          </span>
                        </span>
                        <span className="arr-rail-state"><ArrChapterStateTag ch={c} /></span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
        <button type="button" className="arr-rail-new" onClick={onNew} aria-label="新建章节" title="新建章节，接在当前章后面">
          <I.Plus size={14} /><span>新建章节</span>
        </button>
      </div>
    </aside>
  );
}

export { ArrChapterContext, ArrRail };
