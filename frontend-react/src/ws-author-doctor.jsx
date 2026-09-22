import React from "react";
import { I } from "./icons.jsx";
import { Tag } from "./ws-ui.jsx";
import { chapterLabel } from "./ws-labels.js";

/* ==========================================================
   全书体检 — Book Doctor
   把镜头暴露的问题汇总成一张可操作的待办清单。每项都能点进对应章节、镜头或构思。

   只报读得出来的事实：
   · 章级张力 / 线索在产品里没有编辑入口，这两类检查连同它们的镜头已经删掉；
   · 字数预算只看设过目标的章（目标为 0 时任何字数都会被算成「超额」：真实作品上的「1,803 / 0」）；
   · 视角分布不在这里重复——结构镜头的摘要条和节奏镜头的图例已经各画了一遍；
   · 雪花整理出来的书多三项：构思的改动同步到目录没有、哪几章还没起名、哪几场的三拍还没规划——
     各带一扇直达的门（book / snow / onOpenPlan 由章节编排传入，不传时这三项不出现）。
   ========================================================== */

function arrDeriveIssues(chapters, numOf, extras) {
  const out = [];
  const book = (extras && extras.book) || null;
  const snow = (extras && extras.snow) || null;

  /* 0 · 构思 ↔ 目录（只对雪花整理出来的书） */
  if (book && book.planned) {
    if (snow && snow.pending) {
      out.push({
        kind: "warn", key: "plan-sync", label: "构思的改动待同步", count: snow.pending,
        detail: "构思第 9 / 10 步里改过、目录的场景卡还没跟上的场。写作台和 AI 起草台读的是场景卡。",
        action: { label: snow.busy ? "同步中…" : "同步到目录", run: "sync" },
      });
    }
    if (book.unnamed.length) {
      out.push({
        kind: "warn", key: "plan-unnamed", label: "还没起名的章", count: book.unnamed.length,
        detail: "章名还是系统起的「第 N 章」。点章号进去直接改，或在「整理章节结构」里让 AI 照着各章的场起名。",
        action: { label: "整理章节结构", run: "plan" },
        chips: book.unnamed.map((c) => ({ text: chapterLabel({ n: numOf[c.id] }, { withTitle: false }), go: c.id })),
      });
    }
    if (book.unplanned.length) {
      out.push({
        kind: "warn", key: "plan-unplanned", label: "三拍还没规划的场", count: book.unplanned.length,
        detail: "这些场在构思第 10 步还没写目标 / 冲突 / 挫败（或反应 / 两难 / 决定）。起草之前先把它们规划出来。",
        chips: book.unplanned.slice(0, 12).map((x) => ({ text: `${chapterLabel({ n: numOf[x.chapter.id] }, { withTitle: false })}「${x.scene.title}」`, go: x.chapter.id })),
      });
    }
    if (!(snow && snow.pending) && !book.unnamed.length && !book.unplanned.length) {
      out.push({ kind: "ok", key: "plan", label: "与构思一致", detail: `${book.planChapterCount} 章来自构思的分章，场景卡都已同步，每一场都规划过三拍。` });
    }
  }

  /* 1 · 字数超额：实际明显超出预算（只看设过目标的章） */
  const budgeted = chapters.filter((c) => c.words.target > 0);
  const fat = budgeted.filter((c) => c.words.cur > c.words.target * 1.12);
  if (fat.length) {
    out.push({
      kind: "warn", key: "fat", label: "字数超额", count: fat.length,
      detail: "实际字数明显超出预算，考虑拆分或精简。",
      lens: "pace",
      chips: fat.map((c) => ({ text: `${chapterLabel({ n: numOf[c.id], title: c.title })}：${c.words.cur.toLocaleString()} / ${c.words.target.toLocaleString()} 字`, go: c.id })),
    });
  } else if (budgeted.length) {
    out.push({ kind: "ok", key: "budget", label: "字数预算在轨", detail: "没有已写章节明显超额。" });
  }

  // 待办在前，健康项在后
  const rank = { warn: 0, ok: 1 };
  return out.sort((a, b) => rank[a.kind] - rank[b.kind]);
}

const LENS_LABEL = { pace: "节奏镜头", spine: "结构镜头" };

function ArrDoctor({ chapters, numOf, onOpen, onLens, book, snow, onOpenPlan }) {
  const issues = React.useMemo(() => arrDeriveIssues(chapters, numOf, { book, snow }), [chapters, numOf, book, snow]);
  const runAction = (action) => {
    if (!action) return;
    if (action.run === "sync" && snow && snow.onSync) snow.onSync();
    if (action.run === "plan" && onOpenPlan) onOpenPlan();
  };
  const todo = issues.filter((x) => x.kind === "warn").reduce((s, x) => s + (x.count || 1), 0);

  return (
    <section className="card arr-doctor" aria-labelledby="arr-doctor-title">
      <div className="card-head">
        <div>
          <h2 className="card-title" id="arr-doctor-title">全书体检</h2>
          <div className="card-sub">待办与健康项，点条目直接处理。</div>
        </div>
        <Tag tone={todo ? "warn" : "ok"} className="arr-doc-score">
          {todo ? <><I.AlertTriangle size={13} />{todo} 项待办</> : <><I.Check size={13} />全部健康</>}
        </Tag>
      </div>
      {!issues.length && <p className="arr-sync">这一版目录上没有读得出来的问题。</p>}
      <ul className="arr-doc-list">
        {issues.map((iss) => {
          const Ic = iss.kind === "warn" ? I.AlertTriangle : I.Check;
          return (
            <li key={iss.key} className={`arr-doc s-${iss.kind}`}>
              <span className="arr-doc-ic" aria-hidden="true"><Ic size={14} /></span>
              <div className="arr-doc-body">
                <div className="arr-doc-head">
                  <span className="arr-doc-label">{iss.label}</span>
                  {iss.count != null && <Tag tone="warn" className="arr-doc-count tab-num">{iss.count}</Tag>}
                  <span className="arr-doc-acts">
                    {iss.lens && onLens && (
                      <button type="button" className="btn btn-quiet btn-xs" onClick={() => onLens(iss.lens)}>
                        在{LENS_LABEL[iss.lens] || "镜头"}里看
                      </button>
                    )}
                    {iss.action && (iss.action.run !== "plan" || onOpenPlan) && (
                      <button type="button" className="btn btn-ghost btn-xs" data-testid={`arr-doc-action-${iss.key}`}
                        disabled={iss.action.run === "sync" && snow && snow.busy} onClick={() => runAction(iss.action)}>
                        {iss.action.label}
                      </button>
                    )}
                  </span>
                </div>
                <div className="arr-doc-detail">{iss.detail}</div>
                {iss.chips && (
                  <div className="arr-doc-chips">
                    {iss.chips.map((c, i) => (
                      <button type="button" key={i} className="arr-doc-chip" onClick={() => onOpen(c.go)} title="打开这一章">{c.text}</button>
                    ))}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export { arrDeriveIssues, ArrDoctor };
