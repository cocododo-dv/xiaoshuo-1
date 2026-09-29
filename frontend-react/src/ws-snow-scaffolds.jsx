import React from "react";
import { S2Audience, S2Beats, S2ChapterOutline, S2SynopsisBeats } from "./ws-snow-editors-story.jsx";
import { S2_BACKSTORY_FIELDS, S2_PROFILE_FIELDS, S2CharDeep, S2CharSheet } from "./ws-snow-editors-cast.jsx";
import { S2SceneList } from "./ws-snow-scene-list.jsx";
import { S2ScenePlan } from "./ws-snow-scene-plan.jsx";
import { countChars } from "./lib/text.js";

/* ==========================================================
   雪花十步的编辑器（编辑页）
   ----------------------------------------------------------
   02 一句话概括是自由文本（带长度尺）；其余九步是结构化脚手架：01 读者定位、03 五句骨架、
   04 角色摘要表、05 一页梗概、06 / 08 按角色分栏的深档、07 五段展开 + 章节表；09 在
   ws-snow-scene-list.jsx，10 在 ws-snow-scene-plan.jsx。S2StepEditor 按步骤挑编辑器，并对无关的重渲染（同步状态、健康、回执）免疫。
   ========================================================== */

/* 编辑页的编辑器：有脚手架的步骤用脚手架，否则是自由文本。
   memo：只有这一步的内容、引用的上游脚手架或 AI 忙态变了才重渲染（回调都是稳定引用）。 */
export const S2StepEditor = React.memo(function S2StepEditor({ step, data, draft, setDraft, scaffold, onScaffold, refs, go, ai, onOpenChapterPlan, catalogHasChapters }) {
  if (!data.scaffold) {
    return <S2Edit draft={draft} setDraft={setDraft} stepName={step.name} target={data.target} meter={data.meter} />;
  }
  return <S2Scaffold kind={data.scaffold.type} scaffold={scaffold} onScaffold={onScaffold} refs={refs} go={go} ai={ai} onOpenChapterPlan={onOpenChapterPlan} catalogHasChapters={catalogHasChapters} />;
});

/* ====== Freeform editor (+ optional word meter) ====== */
function S2Edit({ draft, setDraft, stepName, target, meter }) {
  // 字数只报一处、只有一个口径：有长度尺就看尺子，没有才在工具行里报（以前工具行「目标约 60」、尺子「/ 42」、页头「≤25 词」三个数并存）
  const len = countChars(draft);
  return (
    <div className="edit-pane">
      {meter
        ? <S2Meter len={len} target={meter.target} note={meter.note} />
        : <div className="edit-toolbar"><div className="text-muted text-sm">{len} 字{target ? ` · 建议约 ${target} 字` : ""}</div></div>}
      <textarea className="edit-text" aria-label={stepName} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={`在这里写「${stepName}」…`} />
    </div>
  );
}

function S2Meter({ len, target, note }) {
  const pct = Math.min(100, (len / target) * 100);
  const over = len > target;
  return (
    <div className={`sf-meter ${over ? "is-over" : ""}`}>
      <div className="sf-meter-track"><div className="sf-meter-fill" style={{ width: pct + "%" }} /><div className="sf-meter-cap" style={{ left: "100%" }} /></div>
      <div className="sf-meter-foot">
        <span className="sf-meter-count">{len} / {target} 字{over ? "，偏长，再砍一刀" : ""}</span>
        <span className="sf-meter-note">{note}</span>
      </div>
    </div>
  );
}

/* ====== Structured scaffolds ====== */
/* 脚手架上方原来各有一条「说明」横幅，大多是在复述右栏的「本步任务」；现在只留那些说出数据规则的话
   （名册归 04 管、章表可以留空）。 */
function S2Scaffold({ kind, scaffold, onScaffold, refs, go, ai, onOpenChapterPlan, catalogHasChapters }) {
  return (
    <div className="edit-pane">
      {kind === "beats" && <S2Beats scaffold={scaffold} onScaffold={onScaffold} />}
      {kind === "audience" && <S2Audience scaffold={scaffold} onScaffold={onScaffold} />}
      {kind === "charsheet" && <S2CharSheet scaffold={scaffold} onScaffold={onScaffold} ai={ai} />}
      {kind === "synopsisbeats" && <S2SynopsisBeats scaffold={scaffold} onScaffold={onScaffold} refs={refs} />}
      {kind === "chapters" && <S2ChapterOutline scaffold={scaffold} onScaffold={onScaffold} refs={refs} onOpenChapterPlan={onOpenChapterPlan} catalogHasChapters={catalogHasChapters} />}
      {kind === "backstory" && <S2CharDeep scaffold={scaffold} onScaffold={onScaffold} ai={ai} fields={S2_BACKSTORY_FIELDS} roster={(refs && refs.characters) || null} go={go} />}
      {kind === "profile" && <S2CharDeep scaffold={scaffold} onScaffold={onScaffold} ai={ai} fields={S2_PROFILE_FIELDS} roster={(refs && refs.characters) || null} go={go} />}
      {kind === "scenelist" && <S2SceneList scaffold={scaffold} onScaffold={onScaffold} refs={refs} onOpenChapterPlan={onOpenChapterPlan} />}
      {kind === "scene" && <S2ScenePlan scaffold={scaffold} onScaffold={onScaffold} refs={refs} go={go} ai={ai} />}
    </div>
  );
}
