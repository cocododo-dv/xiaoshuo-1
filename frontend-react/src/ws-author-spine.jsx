import React from "react";
import { I } from "./icons.jsx";
import { arrBookFacts, arrBookSpine } from "./ws-author-derive.js";

/* ==========================================================
   结构镜头 — Book Spine（阶段 Z）
   ----------------------------------------------------------
   全书编排的第一张图：卷 → 章 → 场，全部按故事序。它画的是作者在构思里真的做出来的结构——
   三幕、三个灾难各自收束一章、每章装着第几到第几场、每一场是主动还是反应、谁的视角、写到哪了。
   （它替下的「故事弧线」画的是章级张力，而张力在产品里没有任何地方能填：真实作品上永远是一条平线。）
   点章 = 进章节详情；点场 = 进章节详情并落在那一场上。
   ========================================================== */

const SCENE_STATE_LABEL = { done: "已完", writing: "写中", todo: "待写" };

function sceneTip(scene, chapterNum) {
  const head = [
    scene.no ? `第 ${scene.no} 场` : `第 ${chapterNum} 章 · 手加的场`,
    scene.kind,
    scene.pov ? `POV ${scene.pov}` : "",
    SCENE_STATE_LABEL[scene.state] || "",
    scene.mark ? `${scene.mark} · 这一章收在这里` : "",
  ].filter(Boolean).join(" · ");
  return `${head}\n${scene.summary || scene.title || ""}`;
}

function ArrSpineLens({ chapters, numOf, pickedId, onOpen, onOpenScene }) {
  const groups = React.useMemo(() => arrBookSpine(chapters, numOf), [chapters, numOf]);
  const facts = React.useMemo(() => arrBookFacts(chapters), [chapters]);
  const proactive = facts.sceneTotal - facts.reactiveScenes;

  return (
    <div className="arr-spine" data-testid="arr-spine-lens">
      <div className="loom-summary">
        <span className="loom-sum-item"><strong className="tab-num">{chapters.length}</strong> 章</span>
        <span className="loom-sum-item"><strong className="tab-num">{facts.sceneTotal}</strong> 场</span>
        <span className="loom-sum-sep" />
        <span className="loom-sum-item">主动 <span className="tab-num">{proactive}</span> · 反应 <span className="tab-num">{facts.reactiveScenes}</span></span>
        <span className="loom-sum-item">已完 <span className="tab-num">{facts.doneScenes}</span> / {facts.sceneTotal}</span>
        <span className="loom-legend" style={{ marginLeft: "auto" }}>
          {facts.povScenes.slice(0, 5).map((p) => (
            <span key={p.name} title="按场统计的视角分布"><I.Eye size={11} /> {p.name} <span className="tab-num">{p.count}</span></span>
          ))}
        </span>
      </div>

      <div className="arr-spine-acts">
        {groups.map((group) => (
          <section key={group.act.id} className={`arr-spine-act tone-${group.act.tone}`} style={{ flexGrow: Math.max(1, group.chapters.reduce((n, c) => n + Math.max(1, c.scenes.length), 0)) }}>
            <header className="arr-spine-acthead">{group.act.n}</header>
            <div className="arr-spine-chapters">
              {group.chapters.map((c) => (
                <div key={c.id} className={`arr-spine-ch ${c.id === pickedId ? "is-picked" : ""} ${c.current ? "is-current" : ""}`}
                  style={{ flexGrow: Math.max(1, c.scenes.length) }} data-testid="arr-spine-chapter">
                  <button type="button" className="arr-spine-chhead" onClick={() => onOpen(c.id)}
                    title={`第 ${c.num} 章 · ${c.title}${c.rangeLabel ? ` · ${c.rangeLabel}` : ""}`}>
                    <span className="arr-spine-chnum tab-num">{c.num}</span>
                    <span className="arr-spine-chtitle text-serif">{c.title}</span>
                    {c.spine && <span className="arr-spine-mark">{c.spine}</span>}
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
              ))}
            </div>
          </section>
        ))}
      </div>

      <div className="arr-spine-legend">
        <span><i className="arr-spine-key k-proactive" />主动场</span>
        <span><i className="arr-spine-key k-reactive" />反应场</span>
        <span><i className="arr-spine-key k-proactive s-done" />已完</span>
        <span><i className="arr-spine-key k-proactive s-writing" />写中</span>
        <span><i className="arr-spine-key is-hinge" />灾难场 · 收束所在的章</span>
        <span className="arr-spine-legend-note">场上的数字 = 它在构思「场景列表」里的场次</span>
      </div>
    </div>
  );
}

export { ArrSpineLens };
