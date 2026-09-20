import React from "react";
import { I } from "./icons.jsx";
import { arrDeriveThreads } from "./ws-author-loom.jsx";

/* global React, I, arrDeriveThreads */

/* ==========================================================
   全书体检 — Book Doctor
   把镜头暴露的问题汇总成一张可操作的待办清单。每项都能点进对应章节、镜头或构思。

   阶段 Z：只报读得出来的事实。
   · 章级张力 / 线索在产品里没有编辑入口——没有数据时不再报「张力曲线健康」这种假的对勾；
   · 字数预算只看设过目标的章（目标为 0 时任何字数都会被算成「超额」：真实作品上的「1,803 / 0」）；
   · 视角分布按场统计（章级 POV 同样没有编辑入口）；
   · 雪花整理出来的书多三项：构思的改动同步到目录没有、哪几章还没起名、哪几场的三拍还没规划——
     各带一扇直达的门（book / snow / onOpenPlan 由章节编排传入，不传时这三项不出现）。
   ========================================================== */

function arrDeriveIssues(chapters, numOf, extras) {
  const n = chapters.length;
  const threads = arrDeriveThreads(chapters, numOf);
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
        chips: book.unnamed.map((c) => ({ text: `${numOf[c.id]} ${c.title}`, go: c.id })),
      });
    }
    if (book.unplanned.length) {
      out.push({
        kind: "warn", key: "plan-unplanned", label: "三拍还没规划的场", count: book.unplanned.length,
        detail: "这些场在构思第 10 步还没写目标 / 冲突 / 挫折（或反应 / 困境 / 决定）。起草之前先把它们规划出来。",
        chips: book.unplanned.slice(0, 12).map((x) => ({ text: `${numOf[x.chapter.id]} · ${x.scene.title}`, go: x.chapter.id })),
      });
    }
    if (!(snow && snow.pending) && !book.unnamed.length && !book.unplanned.length) {
      out.push({ kind: "ok", key: "plan", label: "与构思一致", detail: `${book.planChapterCount} 章来自构思的分章，场景卡都已同步，每一场都规划过三拍。` });
    }
  }

  /* writing frontier = last chapter that's actually been started (not just planned) */
  let frontier = 0;
  chapters.forEach((c, i) => { if (c.state !== "planned") frontier = i; });

  /* 1 · 悬空线索：未收束，且自写作前沿已 3 章以上没再提及（不含计划在后续收束的） */
  const dangling = threads.filter((t) => !t.closed && t.last.ci <= frontier && (frontier - t.last.ci) >= 3)
    .sort((a, b) => a.last.ci - b.last.ci);
  if (dangling.length) {
    out.push({
      kind: "warn", key: "dangling", label: "悬空线索", count: dangling.length,
      detail: "引入后已多章未提及、且尚未收束。点章号跳到它最后出现的一章，或在织布机里查看。",
      lens: "loom",
      chips: dangling.map((t) => ({ text: `${t.name} · 末见 ${t.last.num}`, go: t.last.chId })),
    });
  }

  /* 2 · 字数超额：实际明显超出预算（只看设过目标的章） */
  const budgeted = chapters.filter((c) => c.words.target > 0);
  const fat = budgeted.filter((c) => c.words.cur > c.words.target * 1.12);
  if (fat.length) {
    out.push({
      kind: "warn", key: "fat", label: "字数超额", count: fat.length,
      detail: "实际字数明显超出预算，考虑拆分或精简。",
      lens: "pace",
      chips: fat.map((c) => ({ text: `${numOf[c.id]} ${c.title} · ${c.words.cur.toLocaleString()}/${c.words.target.toLocaleString()}`, go: c.id })),
    });
  } else if (budgeted.length) {
    out.push({ kind: "ok", key: "budget", label: "字数预算在轨", detail: "没有已写章节明显超额。" });
  }

  /* 3 · 张力回落：同卷内较上一章明显下滑（卷首换场不算）。只有章上真的设过张力才谈。 */
  const hasTension = chapters.some((c) => c.tensionSet);
  const dips = [];
  chapters.forEach((c, i) => {
    if (i === 0 || !hasTension) return;
    const p = chapters[i - 1];
    if (c.act === p.act && (p.tension - c.tension) > 0.12) dips.push({ c, drop: p.tension - c.tension });
  });
  if (dips.length) {
    out.push({
      kind: "warn", key: "dip", label: "张力回落", count: dips.length,
      detail: "同卷内张力较上一章明显下滑，注意是否泄气。",
      lens: "arc",
      chips: dips.map(({ c, drop }) => ({ text: `${numOf[c.id]} ${c.title} · ↓${Math.round(drop * 100)}`, go: c.id })),
    });
  } else if (hasTension) {
    out.push({ kind: "ok", key: "arc", label: "张力曲线健康", detail: "卷内没有意外回落。" });
  }

  /* 4 · 视角分布（信息项）：按场统计——POV 是场上的事实；章级 POV 只有旧数据里才有 */
  const povCounts = {};
  let povUnit = "场";
  chapters.forEach((c) => (c.scenes || []).forEach((s) => { const name = String(s.povName || "").trim(); if (name) povCounts[name] = (povCounts[name] || 0) + 1; }));
  if (!Object.keys(povCounts).length) {
    povUnit = "章";
    chapters.forEach((c) => { const name = String(c.pov || "").trim(); if (name) povCounts[name] = (povCounts[name] || 0) + 1; });
  }
  const povList = Object.entries(povCounts).sort((a, b) => b[1] - a[1]);
  if (povList.length) {
    out.push({ kind: "info", key: "pov", label: "视角分布", detail: povList.map(([p, c]) => `${p} ${c} ${povUnit}`).join(" · "), lens: "pace" });
  }

  /* 5 · 在场线索（信息项）—— 未收束但仍在前沿附近或计划在后续收束 */
  const openRecent = threads.filter((t) => !t.closed).length - dangling.length;
  if (openRecent > 0) {
    out.push({ kind: "info", key: "open", label: "在场线索", detail: `${openRecent} 条线索仍在推进或计划在后续收束，暂不需处理。`, lens: "loom" });
  }

  // warnings first, then ok, then info
  const rank = { warn: 0, ok: 1, info: 2 };
  return out.sort((a, b) => rank[a.kind] - rank[b.kind]);
}

function ArrDoctor({ chapters, numOf, onOpen, onLens, book, snow, onOpenPlan }) {
  const issues = React.useMemo(() => arrDeriveIssues(chapters, numOf, { book, snow }), [chapters, numOf, book, snow]);
  const runAction = (action) => {
    if (!action) return;
    if (action.run === "sync" && snow && snow.onSync) snow.onSync();
    if (action.run === "plan" && onOpenPlan) onOpenPlan();
  };
  const todo = issues.filter((x) => x.kind === "warn").reduce((s, x) => s + (x.count || 1), 0);

  return (
    <section className="card arr-doctor">
      <div className="card-head">
        <div>
          <div className="card-title">全书体检</div>
          <div className="card-sub">从三个镜头汇总的待办与健康项。点条目直接处理。</div>
        </div>
        <span className={`arr-doc-score ${todo ? "is-todo" : "is-clear"}`}>
          {todo ? <React.Fragment><I.AlertTriangle size={13} />{todo} 项待办</React.Fragment>
                : <React.Fragment><I.Check size={13} />全部健康</React.Fragment>}
        </span>
      </div>
      <ul className="arr-doc-list">
        {issues.map((iss) => {
          const Ic = iss.kind === "warn" ? I.AlertTriangle : iss.kind === "ok" ? I.Check : I.Info || I.Circle;
          return (
            <li key={iss.key} className={`arr-doc s-${iss.kind}`}>
              <span className="arr-doc-ic"><Ic size={14} /></span>
              <div className="arr-doc-body">
                <div className="arr-doc-head">
                  <span className="arr-doc-label">{iss.label}</span>
                  {iss.count != null && <span className="arr-doc-count tab-num">{iss.count}</span>}
                  {iss.lens && onLens && (
                    <button className="arr-doc-lens" onClick={() => onLens(iss.lens)}>
                      在{iss.lens === "loom" ? "织布机" : iss.lens === "pace" ? "节奏镜头" : "弧线"}查看 →
                    </button>
                  )}
                  {iss.action && (iss.action.run !== "plan" || onOpenPlan) && (
                    <button className="arr-doc-lens" data-testid={`arr-doc-action-${iss.key}`}
                      disabled={iss.action.run === "sync" && snow && snow.busy} onClick={() => runAction(iss.action)}>
                      {iss.action.label} →
                    </button>
                  )}
                </div>
                <div className="arr-doc-detail">{iss.detail}</div>
                {iss.chips && (
                  <div className="arr-doc-chips">
                    {iss.chips.map((c, i) => (
                      <button key={i} className="arr-doc-chip" onClick={() => onOpen(c.go)} title="跳到该章">{c.text}</button>
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

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { arrDeriveIssues, ArrDoctor };
