import React from "react";
import { I } from "./icons.jsx";
import { S2AutoText, S2PovPick } from "./ws-snow-fields.jsx";
import {
  S2_SPINE_OPTS, s2InferSpine, s2LineStats, s2NextSceneRowId, s2PacingRuns, s2ReorderScenes, s2RosterList, s2SceneNo,
} from "./ws-snow-model.js";

/* ==========================================================
   09 场景列表（从 ws-snow-scenes.jsx 拆出，2026-09-22）
   ----------------------------------------------------------
   一行一场的电子表格：主线与支线在这里编织，道德前提读 03 的活数据，默认视角取 04 名册的主角；
   可以在某一场后面插一场、拖拽换位（阶段 M）。反应场是少数，只在下一目标不明显时才写。
   ========================================================== */

const { useState: useSS } = React;

const S2_LINE_TONES = ["gold", "slate", "sage"];  // 非主线循环配色
const S2_KIND_LABEL = { main: "主线", thread: "线索", sub: "支线" };

export function S2SceneList({ scaffold, onScaffold, refs, onOpenChapterPlan }) {
  const list = scaffold.list || [];
  const lines = (scaffold.lines && scaffold.lines.length) ? scaffold.lines : [{ id: "main", name: "主线", kind: "main", tone: "crimson", refract: "" }];
  const roster = s2RosterList(refs);
  const placeOpts = [...new Set(list.map(s => (s.place || "").trim()).filter(Boolean))];
  const [hiLine, setHiLine] = useSS(null);
  /* 道德前提读 03 的活数据，不再用静态种子；默认 POV 取 04 名册的主角 */
  const para = (refs && refs.paragraph) || {};
  const premise = { f: (para.premiseF || "").trim(), t: (para.premiseT || "").trim() };
  const mainCharId = (() => {
    const entry = roster.find(r => {
      const chars = ((refs && refs.characters) || {}).chars || {};
      return (chars[r.id] || {}).role === "主角";
    }) || roster[0];
    return (entry && entry.id) || "";
  })();

  const setScene = (i, f, v) => onScaffold(s => ({ ...s, list: s.list.map((sc, j) => j === i ? { ...sc, [f]: v } : sc) }));
  const addScene = () => onScaffold(s => ({
    ...s,
    list: [...s.list, { id: s2NextSceneRowId(s.list), type: "proactive", line: hiLine || "main", pov: mainCharId, place: "", event: "", crucible: "", fn: "", spine: "" }],
  }));
  const delScene = (i) => onScaffold(s => ({ ...s, list: s.list.filter((_, j) => j !== i) }));
  const moveScene = (i, d) => onScaffold(s => {
    const j = i + d; if (j < 0 || j >= s.list.length) return s;
    const l = s.list.slice(); [l[i], l[j]] = [l[j], l[i]]; return { ...s, list: l };
  });
  // 阶段 M：原著的场景表是可以随手挪动的电子表格——在某一场后面插一场、拖拽换位
  const insertSceneAfter = (i) => onScaffold(s => {
    const prev = s.list[i] || {};
    const fresh = { id: s2NextSceneRowId(s.list), type: "proactive", line: prev.line || hiLine || "main", pov: prev.pov || mainCharId, place: prev.place || "", event: "", crucible: "", fn: "", spine: "" };
    return { ...s, list: [...s.list.slice(0, i + 1), fresh, ...s.list.slice(i + 1)] };
  });
  const [dragIdx, setDragIdx] = useSS(null);
  /* 只有按住左侧把手时这一行才可拖：以前整行 draggable，在输入框里拖选文字会变成拖动整行。
     拖放事件仍挂在行上（单测直接在行上派发 dragstart / drop）。 */
  const [gripArmed, setGripArmed] = useSS(null);
  const dropOn = (i) => { if (dragIdx == null || dragIdx === i) { setDragIdx(null); return; } onScaffold(s => ({ ...s, list: s2ReorderScenes(s.list, dragIdx, i) })); setDragIdx(null); };

  const setLine = (id, f, v) => onScaffold(s => ({ ...s, lines: (s.lines || []).map(ln => ln.id === id ? { ...ln, [f]: v } : ln) }));
  const addLine = () => onScaffold(s => {
    const subs = (s.lines || []).filter(l => l.kind !== "main").length;
    return { ...s, lines: [...(s.lines || []), { id: "L" + Date.now().toString(36).slice(-4), name: "新支线", kind: "sub", tone: S2_LINE_TONES[subs % S2_LINE_TONES.length], refract: "" }] };
  });
  const delLine = (id) => onScaffold(s => ({
    ...s,
    lines: (s.lines || []).filter(l => l.id !== id),
    list: (s.list || []).map(sc => (sc.line === id ? { ...sc, line: "main" } : sc)),
  }));
  const toneOf = (id) => (lines.find(l => l.id === id) || {}).tone || "slate";

  const pacing = s2PacingRuns(list);
  const lineStats = s2LineStats(list, lines);
  const pro = list.filter(s => s.type === "proactive").length;
  const rea = list.length - pro;
  const noCrucible = list.filter(s => !(s.crucible || "").trim()).length;
  // 灾难场：显式标记优先，没有标记时从「功能」里认（与分章面板、后端同一规则；只显示，不写回）
  const inferredOf = (s) => (s.spine ? "" : s2InferSpine(s.fn));
  const spineHit = list.filter(s => s.spine || inferredOf(s)).length;
  const inferredHit = list.filter(s => inferredOf(s)).length;
  const tightMax = pacing.tight.length ? Math.max(...pacing.tight.map(r => r.len)) : 0;
  const slackMax = pacing.slack.length ? Math.max(...pacing.slack.map(r => r.len)) : 0;
  /* 织线与节奏：只有主线时默认收起（没有线可编织，只剩一条节奏带），有支线时默认展开 */
  const hasSubplots = lines.some(l => l.kind !== "main");
  const [weaveOpen, setWeaveOpen] = useSS(hasSubplots);
  const paceFlag = tightMax
    ? { tone: "gold", text: `连续 ${tightMax} 场主动，很长一段没有喘息——若下一目标不明显，考虑插一场反应场（也可只写两段概述）` }
    : slackMax
      ? { tone: "gold", text: `连续 ${slackMax} 场反应，节奏松了，推进一场主动` }
      : { tone: "sage", text: "节奏可行：反应场是少数，只在下一目标不明显时才写" };

  return (
    <div className="sf-scaffold sf-scenelist">
      <div className="sf-scene-stats">
        <span className="sf-sstat"><b>{list.length}</b> 场</span>
        <span className="sf-sstat tone-crimson"><b>{pro}</b> 主动</span>
        <span className="sf-sstat tone-slate"><b>{rea}</b> 反应</span>
        <span className="sf-sstat tone-gold" title={inferredHit ? `其中 ${inferredHit} 场没有显式标记，是从「功能」一栏认出来的` : "标了灾难的场（三个灾难各收束一章）"}><b>{spineHit}</b> 灾难场</span>
        <span className="sf-sstat"><b>{lines.length}</b> 条线</span>
        <span className={`sf-sstat ${noCrucible ? "tone-rose" : "tone-sage"}`}>{noCrucible ? <><I.AlertTriangle size={11} /> {noCrucible} 场还没写坩埚</> : list.length ? <><I.Check size={11} /> 场场有冲突</> : null}</span>
      </div>

      <section className={`sf-weave ${weaveOpen ? "is-open" : "is-closed"}`}>
        <div className="sf-weave-head">
          <button type="button" className="sf-weave-toggle" aria-expanded={weaveOpen} onClick={() => setWeaveOpen(o => !o)}>
            <I.ChevronRight size={13} className="sf-weave-chev" /> 织线与节奏
          </button>
          {!weaveOpen && <span className={`sf-flag tone-${paceFlag.tone}`}>{paceFlag.tone === "sage" ? <I.Check size={10} /> : <I.AlertTriangle size={10} />} {tightMax ? `连续 ${tightMax} 场主动` : slackMax ? `连续 ${slackMax} 场反应` : "节奏可行"}</span>}
          <span className="sf-weave-premise" title="每条线都应折射这条道德前提（来自 03 的中点翻转），折射不出来的线可能是闲笔">
            <span className="sf-wp-false">{premise.f || "（03 还没写错误信念）"}</span>
            <I.ChevronRight size={11} />
            <span className="sf-wp-true">{premise.t || "（正确信念）"}</span>
          </span>
        </div>

        {weaveOpen && (
          <React.Fragment>
            <div className="sf-weave-rhythm">
              <span className="sf-weave-axis">节奏</span>
              <div className="sf-rhythm-band" aria-hidden="true">
                {list.map((s, i) => (
                  <span key={s.id || i}
                    className={`sf-rb-cell ${s.type === "proactive" ? "is-pro" : "is-rea"} ${hiLine && (s.line || "main") !== hiLine ? "is-dim" : ""}`}
                    title={`${s2SceneNo(s.id, i)} · ${s.type === "proactive" ? "主动" : "反应"}`} />
                ))}
              </div>
              <div className="sf-rhythm-flags">
                <span className={`sf-flag tone-${paceFlag.tone}`}>{paceFlag.tone === "sage" ? <I.Check size={10} /> : <I.AlertTriangle size={10} />} {paceFlag.text}</span>
              </div>
            </div>

            <div className="sf-weave-lines">
              {lineStats.map(ln => (
                <div key={ln.id} className={`sf-wl-row tone-${ln.tone} ${hiLine === ln.id ? "is-hi" : ""} ${hiLine && hiLine !== ln.id ? "is-dim" : ""}`}>
                  {/* 高亮开关与线名输入并排，不再把输入框套在按钮里（Firefox 里那样打不进字） */}
                  <div className="sf-wl-tab">
                    <button type="button" className="sf-wl-hi" aria-pressed={hiLine === ln.id} onClick={() => setHiLine(hiLine === ln.id ? null : ln.id)}
                      aria-label={`只看「${ln.name}」这条线的场景`} title="高亮这条线的场景"><span className="sf-wl-dot" /></button>
                    <input className="sf-wl-name" aria-label="线名" value={ln.name} onChange={(e) => setLine(ln.id, "name", e.target.value)} />
                    <span className="sf-wl-kind">{S2_KIND_LABEL[ln.kind] || "支线"}</span>
                  </div>
                  <div className="sf-wl-track" aria-hidden="true">
                    {list.map((s, i) => <span key={s.id || i} className={`sf-wl-cell ${(s.line || "main") === ln.id ? "is-on" : ""}`} title={s2SceneNo(s.id, i)} />)}
                  </div>
                  <div className="sf-wl-meta">
                    {ln.count ? <span className="sf-wl-count">{ln.count}/{list.length}</span> : <span className="sf-wl-count is-empty">未编入</span>}
                    {ln.clustered ? <span className="sf-flag tone-gold" title="这条线挤在很窄的一段里——像绕路而不是编织，考虑分散穿插"><I.AlertTriangle size={10} /> 扎堆</span> : null}
                  </div>
                  <input className="sf-wl-refract" value={ln.refract} aria-label={`「${ln.name}」如何折射道德前提`}
                    onChange={(e) => setLine(ln.id, "refract", e.target.value)}
                    placeholder={ln.kind === "main" ? "主线如何兑现道德前提…" : "这条线如何折射道德前提？（填不出来，可能是闲笔）"} />
                  {ln.kind !== "main"
                    ? <button className="sf-wl-del" onClick={() => delLine(ln.id)} aria-label={`删除「${ln.name}」这条线`} title="删除这条线"><I.X size={12} /></button>
                    : <span className="sf-wl-del-sp" />}
                </div>
              ))}
              <button className="sf-wl-add" onClick={addLine}><I.Plus size={12} /> 添加支线</button>
            </div>
          </React.Fragment>
        )}
      </section>

      <div className="sf-scene-table" role="list" aria-label="场景列表">
        {list.map((s, i) => {
          const lt = toneOf(s.line || "main");
          const dim = hiLine && (s.line || "main") !== hiLine;
          const no = s2SceneNo(s.id, i);
          const inferred = inferredOf(s);
          // 阶段 M：章归属只读——同一章的第一场前插一行章头（章在分章面板里改，这里只看）
          const chapterHead = s.chapter && s.chapter !== ((list[i - 1] || {}).chapter || "") ? s.chapter : "";
          return (
          <React.Fragment key={s.id || i}>
          {chapterHead ? (
            <button type="button" className="sf-scene-chapter" data-testid={`snow-scene-chapter-${i}`}
              title="章归属在分章面板里改——点这里打开它（拆章 / 并章 / 挪章界 / 改章名）"
              onClick={() => onOpenChapterPlan && onOpenChapterPlan()}>
              <span>{chapterHead}</span><I.Layout size={11} />
            </button>
          ) : null}
          <div role="listitem" data-testid={`snow-scene-row-${i}`} draggable={gripArmed === i}
            onDragStart={(e) => { setDragIdx(i); try { if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; } catch (err) {} }}
            onDragOver={(e) => e.preventDefault()} onDrop={() => dropOn(i)} onDragEnd={() => { setDragIdx(null); setGripArmed(null); }}
            className={`sf-scene-row line-${lt} ${(s.spine || inferred) ? "is-spine" : ""} ${!(s.crucible || "").trim() ? "is-nocru" : ""} ${dim ? "is-dim" : ""} ${dragIdx === i ? "is-dragging" : ""}`}>
            <button type="button" className="sc-grip" aria-label={`${no}：按住拖动换位置，或按 ↑ / ↓ 移动`} title="按住拖动换位置；聚焦后按 ↑ / ↓ 移动"
              onPointerDown={() => setGripArmed(i)} onPointerUp={() => setGripArmed(null)} onPointerCancel={() => setGripArmed(null)}
              onKeyDown={(e) => {
                if (e.key === "ArrowUp") { e.preventDefault(); moveScene(i, -1); }
                else if (e.key === "ArrowDown") { e.preventDefault(); moveScene(i, 1); }
              }}>
              <I.GripVertical size={14} />
            </button>
            <div className="sc-meta">
              <span className="sc-no" title={s.id}>{no}</span>
              <button type="button" className={`sc-type ${s.type === "proactive" ? "is-pro" : "is-rea"}`} onClick={() => setScene(i, "type", s.type === "proactive" ? "reactive" : "proactive")}
                title="切换 主动（目标-冲突-挫败）/ 反应（反应-两难-决定）">
                {s.type === "proactive" ? "主动" : "反应"}
              </button>
              <select className={`sc-line tone-${lt}`} value={s.line || "main"} onChange={(e) => setScene(i, "line", e.target.value)} aria-label={`${no} 服务哪条线`} title="这一场服务哪条线">
                {lines.map(ln => <option key={ln.id} value={ln.id}>{ln.name}</option>)}
              </select>
              <S2PovPick value={s.pov} roster={roster} onChange={(v) => setScene(i, "pov", v)} className="sc-in sc-pov" ariaLabel={`${no} 的视角人物`} />
              <input className="sc-in sc-in-place" list="s2-place-opts" value={s.place || ""} onChange={(e) => setScene(i, "place", e.target.value)} placeholder="地点" aria-label={`${no} 地点`} />
              <input className="sc-in sc-in-fn" value={s.fn || ""} onChange={(e) => setScene(i, "fn", e.target.value)} placeholder="功能（如 起势 / 灾一）" aria-label={`${no} 在全书里的功能`} />
              <select className={`sc-spine ${inferred ? "is-inferred" : ""}`} value={s.spine || ""} onChange={(e) => setScene(i, "spine", e.target.value)}
                aria-label={`${no} 的灾难标记`} title={inferred ? `没有显式标记；按「功能」一栏认作${inferred}（分章时也这么认）。要改就在这里选` : "这一场是不是三个灾难之一"}>
                <option value="">{inferred ? `${inferred}（推断）` : "非灾难"}</option>
                {S2_SPINE_OPTS.filter(Boolean).map(o => <option key={o} value={o}>{o}</option>)}
              </select>
              <span className="sc-c-act">
                <button type="button" className="sc-act" data-testid={`snow-scene-insert-${i}`} onClick={() => insertSceneAfter(i)} aria-label={`在 ${no} 后面插一场`} title="在这一场后面插一场"><I.Plus size={13} /></button>
                <button type="button" className="sc-act sc-act-del" onClick={() => delScene(i)} aria-label={`删除 ${no}`} title="删除这一场"><I.X size={13} /></button>
              </span>
            </div>
            <div className="sc-story">
              <label className="sc-story-field">
                <span className="sc-story-k">事件</span>
                <S2AutoText className="sc-in sc-in-event" value={s.event} onChange={(e) => setScene(i, "event", e.target.value)} placeholder="这一场发生什么" />
              </label>
              <label className="sc-story-field">
                <span className="sc-story-k">坩埚</span>
                <S2AutoText className="sc-in sc-in-cru" value={s.crucible} onChange={(e) => setScene(i, "crucible", e.target.value)} placeholder="什么把角色困在这里、退不出去" />
              </label>
            </div>
          </div>
          </React.Fragment>
          );
        })}
      </div>
      <datalist id="s2-place-opts">{placeOpts.map(p => <option key={p} value={p} />)}</datalist>
      <button className="sf-scene-add" onClick={addScene}><I.Plus size={14} /> 添加场景</button>
    </div>
  );
}
