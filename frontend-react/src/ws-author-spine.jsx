import React from "react";
import { I } from "./icons.jsx";
import { Tag } from "./ws-ui.jsx";
import { arrBookFacts, arrBookSpine } from "./ws-author-derive.js";
import { ARR_SCENE_STATE } from "./ws-author-data.jsx";
import { chapterHeading, chapterLabel } from "./ws-labels.js";

/* ==========================================================
   结构镜头 — Book Spine（阶段 Z）
   ----------------------------------------------------------
   全书编排的第一张图：卷 → 章 → 场，全部按故事序。它画的是作者在构思里真的做出来的结构——
   三幕、三个灾难各自收束一章、每章装着第几到第几场、每一场是主动还是反应、谁的视角、写到哪了。
   摘要条是全书唯一的一行数：章 / 场 / 字数 / 写完几场 / 视角分布（体检和统计块不再各报一遍）。
   点章 = 进章节详情；点场 = 进章节详情并落在那一场上。
   ========================================================== */

function sceneTip(scene, chapterNum) {
  const head = [
    scene.no ? `第 ${scene.no} 场` : `${chapterLabel({ n: chapterNum }, { withTitle: false })} · 手加的场`,
    scene.kind,
    scene.pov ? `视角 ${scene.pov}` : "",
    (ARR_SCENE_STATE[scene.state] || {}).label || "",
    scene.mark ? `${scene.mark} · 这一章收在这里` : "",
  ].filter(Boolean).join(" · ");
  return `${head}\n${scene.summary || scene.title || ""}`;
}

function ArrSpineLens({ chapters, numOf, pickedId, onOpen, onOpenScene }) {
  const groups = React.useMemo(() => arrBookSpine(chapters, numOf), [chapters, numOf]);
  const facts = React.useMemo(() => arrBookFacts(chapters), [chapters]);
  const proactive = facts.sceneTotal - facts.reactiveScenes;
  const wordsPct = facts.wordsTarget > 0 ? Math.min(100, Math.round((facts.words / facts.wordsTarget) * 100)) : null;

  return (
    <div className="arr-spine" data-testid="arr-spine-lens">
      <div className="arr-lens-summary">
        <span className="arr-lens-sum-item"><strong className="tab-num">{chapters.length}</strong> 章</span>
        <span className="arr-lens-sum-item"><strong className="tab-num">{facts.sceneTotal}</strong> 场</span>
        <span className="arr-lens-sum-item" title={wordsPct != null ? `全书目标 ${facts.wordsTarget.toLocaleString()} 字` : "字数取自各场已保存的正文"}>
          <strong className="tab-num">{facts.words.toLocaleString()}</strong> 字{wordsPct != null ? <span className="tab-num"> · 目标的 {wordsPct}%</span> : null}
        </span>
        <span className="arr-lens-sum-sep" aria-hidden="true" />
        <span className="arr-lens-sum-item">{ARR_SCENE_STATE.done.label} <strong className="tab-num">{facts.doneScenes}</strong> / {facts.sceneTotal} 场</span>
        <span className="arr-lens-sum-item">主动 <span className="tab-num">{proactive}</span> · 反应 <span className="tab-num">{facts.reactiveScenes}</span></span>
        {facts.povScenes.length ? (
          <span className="arr-lens-legend" aria-label="按场统计的视角分布">
            {facts.povScenes.slice(0, 5).map((p) => (
              <span key={p.name} title={`${p.name}：${p.count} 场`}><I.Eye size={11} /> {p.name} <span className="tab-num">{p.count} 场</span></span>
            ))}
          </span>
        ) : null}
      </div>

      <div className="arr-spine-acts">
        {groups.map((group) => (
          <section key={group.act.id} className="arr-spine-act" data-tone={group.act.tone} aria-label={group.act.n}
            style={{ flexGrow: Math.max(1, group.chapters.reduce((n, c) => n + Math.max(1, c.scenes.length), 0)) }}>
            <header className="arr-spine-acthead">{group.act.n}</header>
            <div className="arr-spine-chapters">
              {group.chapters.map((c) => {
                // 章号「第 N 章」只在有真章名时作小字；占位名（第 N 章 / 未命名）只写一遍
                const head = chapterHeading({ n: c.num, title: c.title });
                return (
                  <div key={c.id} className={`arr-spine-ch ${c.id === pickedId ? "is-picked" : ""} ${c.current ? "is-current" : ""}`}
                    style={{ flexGrow: Math.max(1, c.scenes.length) }} data-testid="arr-spine-chapter">
                    <button type="button" className="arr-spine-chhead" onClick={() => onOpen(c.id)}
                      title={`${chapterLabel({ n: c.num, title: c.title }, { maxTitle: Infinity })}${c.rangeLabel ? ` · ${c.rangeLabel}` : ""}`}>
                      {head.title && <span className="arr-spine-chnum tab-num">{head.num}</span>}
                      <span className="arr-spine-chtitle text-serif">{head.title || head.num}</span>
                      {c.spine && <Tag tone="warn" className="arr-spine-mark">{c.spine}</Tag>}
                    </button>
                    <div className="arr-spine-cells">
                      {c.scenes.map((s) => (
                        <button type="button" key={s.sid}
                          className={`arr-spine-cell k-${s.kind === "反应" ? "reactive" : "proactive"} s-${s.state} ${s.mark ? "is-hinge" : ""} ${s.handMade ? "is-handmade" : ""}`}
                          title={sceneTip(s, c.num)} aria-label={sceneTip(s, c.num).split("\n")[0]}
                          onClick={() => (onOpenScene ? onOpenScene(c.id, s.index) : onOpen(c.id))}>
                          {s.no ? <span className="tab-num">{s.no}</span> : <I.Plus size={9} />}
                        </button>
                      ))}
                      {!c.scenes.length && <span className="arr-spine-empty">还没有场</span>}
                    </div>
                    <div className="arr-spine-chfoot">{c.rangeLabel || `${c.scenes.length} 场`}</div>
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>

      <div className="arr-spine-legend">
        <span><i className="arr-spine-key k-proactive" />主动场</span>
        <span><i className="arr-spine-key k-reactive" />反应场</span>
        <span><i className="arr-spine-key k-proactive s-done" />{ARR_SCENE_STATE.done.label}</span>
        <span><i className="arr-spine-key k-proactive s-writing" />{ARR_SCENE_STATE.writing.label}</span>
        <span><i className="arr-spine-key is-hinge" />灾难场，收束所在的章</span>
        <span className="arr-spine-legend-note">场上的数字是它在构思「场景列表」里的场次</span>
      </div>
    </div>
  );
}

export { ArrSpineLens };
