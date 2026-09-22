import React from "react";
import { I } from "./icons.jsx";
import { chapterNoInTitle } from "./ws-snow-chapters-model.js";

/* ==========================================================
   分章面板的展示件（从 ws-snow-chapters.jsx 拆出，2026-09-22）
   ----------------------------------------------------------
   一章（章头 · 章摘要 · 场列表与挪章界按钮）、「未分配」区、提醒区。
   只画、只回调：状态、确认前的「还没确认的调整会丢」追问都在面板里。
   ========================================================== */

const sceneText = (scene) => (scene.title && scene.title !== scene.summary ? scene.title : (scene.summary || scene.title));

/* 场上的数字是它在构思「场景列表」里的序号（故事序），不是章内第几场——悬停说清楚 */
function StoryNo({ scene }) {
  return (
    <span className="sf-chapterplan-no" title={scene.storyIndex ? `构思「场景列表」里的第 ${scene.storyIndex} 场` : "还没排进场景列表"}>
      {scene.storyIndex || "·"}
    </span>
  );
}

function SceneMain({ scene }) {
  return (
    <span className="sf-chapterplan-scenemain">
      {scene.fn && <span className="sf-chapterplan-fn">{scene.fn}</span>}
      <span className="sf-chapterplan-scenetitle" title={scene.summary || scene.title}>{sceneText(scene)}</span>
    </span>
  );
}

/* 一章。chapter.index 是它在章表里的位置；挪章界只给两种合法的挪法（首场并入上一章、末场移到下一章）。
   onGoToScene 为空时不给「在构思里改这一场」（宿主没有接这扇门）。 */
export function ChapterPlanChapter({ chapter, isLastChapter, saving, onSetField, onMerge, onMove, onSplit, onGoToScene }) {
  const first = chapter.scenes[0];
  const last = chapter.scenes[chapter.scenes.length - 1];
  const range = first && first.storyIndex
    ? (first === last ? `第 ${first.storyIndex} 场` : `第 ${first.storyIndex}–${last.storyIndex} 场`)
    : "";
  return (
    <div className={`sf-chapterplan-chapter ${chapter.scenes.length ? "" : "is-empty"}`}
      data-testid={`chapter-plan-chapter-${chapter.index}`}>
      <div className="sf-chapterplan-chaphead">
        {/* 章号「第 N 章」：章名框里已经是「第 N 章」这种占位（或空着、占位提示里带着章号）时不再并排写一遍 */}
        {!chapterNoInTitle(chapter.title, chapter.index) && (
          <span className="sf-chapterplan-chapno">第 {chapter.index + 1} 章</span>
        )}
        <input
          className="sf-chapterplan-title"
          value={chapter.title}
          placeholder={`第 ${chapter.index + 1} 章（未命名）`}
          onChange={e => onSetField("title", e.target.value)}
          aria-label={`第 ${chapter.index + 1} 章标题`}
        />
        {chapter.spine && <span className="sf-chapterplan-spine" title="这一章收束在这个灾难上">{chapter.spine}</span>}
        <span className="sf-chapterplan-count">{chapter.scenes.length} 场{range ? ` · ${range}` : ""}</span>
        {chapter.index > 0 && (
          <button className="btn btn-ghost btn-xs" disabled={saving}
            title="与上一章合并：这一章的场接到上一章后面" aria-label="与上一章合并"
            data-testid={`chapter-plan-merge-${chapter.index}`}
            onClick={onMerge}>
            <I.ChevronUp size={12} /> 并入上一章
          </button>
        )}
      </div>
      {!!chapter.scenes.length && (
        <input
          className="sf-chapterplan-sum"
          value={chapter.summary || ""}
          placeholder="这一章把局面推到哪——一句话（留空就取章末那一场）"
          onChange={e => onSetField("summary", e.target.value)}
          aria-label={`第 ${chapter.index + 1} 章摘要`}
          data-testid={`chapter-plan-summary-${chapter.index}`}
        />
      )}
      <ul className="sf-chapterplan-scenes">
        {chapter.scenes.map((scene, si) => {
          const isFirst = si === 0;
          const isLast = si === chapter.scenes.length - 1;
          return (
            <li key={scene.scenePlanId} className="sf-chapterplan-scene">
              <StoryNo scene={scene} />
              <span className={`sf-chapterplan-kind is-${scene.primaryForm}`}>
                {scene.primaryForm === "reactive" ? "反应" : "主动"}
              </span>
              <SceneMain scene={scene} />
              {scene.spine && <span className="sf-chapterplan-anchor" title="灾难场：它收束所在的章">●{scene.spine}</span>}
              {!scene.planned && <span className="sf-chapterplan-unplanned" title="第 10 步还没规划三拍">未规划</span>}
              <span className="sf-chapterplan-sceneacts">
                {onGoToScene && scene.sceneId && (
                  <button className="btn btn-ghost btn-xs" disabled={saving}
                    title="去构思第 10 步改这一场的设计（形态 / 三拍 / 视角）" aria-label="在构思里改这一场"
                    data-testid={`chapter-plan-scene-edit-${scene.storyIndex || si}`}
                    onClick={() => onGoToScene(scene)}>
                    <I.Pen size={12} />
                  </button>
                )}
                {isFirst && chapter.index > 0 && (
                  <button className="btn btn-ghost btn-xs" disabled={saving}
                    title="这一章的第一场并入上一章（成为上一章的最后一场）" aria-label="并入上一章"
                    onClick={() => onMove(si, chapter.index - 1)}>
                    <I.ChevronUp size={12} />
                  </button>
                )}
                {!isFirst && (
                  <button className="btn btn-ghost btn-xs" disabled={saving}
                    title="从这一场另起一章：它和后面的场搬进一章新章" aria-label="从这里另起一章"
                    data-testid={`chapter-plan-split-${chapter.index}-${si}`}
                    onClick={() => onSplit(si)}>
                    <I.Scissors size={12} />
                  </button>
                )}
                {isLast && !isLastChapter && (
                  <button className="btn btn-ghost btn-xs" disabled={saving}
                    title="这一章的最后一场移到下一章（成为下一章的第一场）" aria-label="移到下一章"
                    onClick={() => onMove(si, chapter.index + 1)}>
                    <I.ChevronDown size={12} />
                  </button>
                )}
              </span>
            </li>
          );
        })}
        {!chapter.scenes.length && <li className="sf-chapterplan-scene is-placeholder">（没有分到场 · 不会写入目录）</li>}
      </ul>
    </div>
  );
}

/* 还没分到章的场：每一场只有一个去处——它在场景列表里前一场所在的章 */
export function ChapterPlanUnassigned({ scenes, canAssign, onAssign }) {
  return (
    <div className="sf-chapterplan-chapter is-unassigned">
      <div className="sf-chapterplan-chaphead">
        <span className="sf-chapterplan-title as-text">未分配</span>
        <span className="sf-chapterplan-count">{scenes.length} 场</span>
      </div>
      <ul className="sf-chapterplan-scenes">
        {scenes.map((scene, si) => (
          <li key={scene.scenePlanId} className="sf-chapterplan-scene">
            <StoryNo scene={scene} />
            <SceneMain scene={scene} />
            <button className="btn btn-quiet btn-xs" disabled={!canAssign}
              title="归入场景列表里它前一场所在的章"
              onClick={() => onAssign(si, scene)}>归入它前一场的章</button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* 提醒区：后端的阻断 / 提醒、「还有 N 场没分到章」、整理前检查（物化闸门）。每一条能就地解决的都带着那个动作——
   孤儿场的 blocker 明说「请先决定是一并删除还是保留」，那两个决定必须真的在这里，否则就是个无解的死结。
   什么都没有时不渲染。 */
export function ChapterPlanWarnings({
  blockers, advisories, gateBlockers, gateAdvisories, unassignedCount,
  busy, saving, resolving, onResolveOrphan, onFixOrder, onGoToGateItem,
}) {
  if (!(blockers.length || advisories.length || gateBlockers.length || gateAdvisories.length || unassignedCount)) return null;
  return (
    <div className="sf-chapterplan-warnings" data-testid="chapter-plan-warnings">
      {blockers.map((w, i) => (
        <div key={`b${i}`} className="sf-chapterplan-warn tone-rose">
          <I.AlertTriangle size={12} /> {w.message}
          {w.kind === "orphaned_scene" && w.scene_plan_id ? (
            <span className="sf-chapterplan-warn-actions">
              <button className="btn btn-quiet btn-xs" disabled={!!resolving}
                onClick={() => onResolveOrphan(w.scene_plan_id, "keep")}>
                保留正文
              </button>
              <button className="btn btn-quiet btn-xs" disabled={!!resolving}
                onClick={() => onResolveOrphan(w.scene_plan_id, "discard")}>
                一并删除
              </button>
            </span>
          ) : null}
        </div>
      ))}
      {!!unassignedCount && (
        <div className="sf-chapterplan-warn tone-rose">
          <I.AlertTriangle size={12} /> 还有 {unassignedCount} 场没有分到章 —— 指派完才能写入。
        </div>
      )}
      {advisories.map((w, i) => (
        <div key={`w${i}`} className="sf-chapterplan-warn tone-gold">
          <I.AlertTriangle size={12} /> {w.message}
          {w.kind === "chapter_order_conflict" ? (
            <span className="sf-chapterplan-warn-actions">
              <button className="btn btn-quiet btn-xs" disabled={busy || saving}
                data-testid="chapter-plan-fix-order"
                onClick={onFixOrder}>
                按场景重新分章
              </button>
            </span>
          ) : null}
        </div>
      ))}
      {gateBlockers.map((item, i) => (
        <div key={item.id || `gb${i}`} className="sf-chapterplan-warn tone-rose" role="alert"
          data-testid="materialization-gate-blocker">
          <I.AlertTriangle size={12} /> {item.message}
          {onGoToGateItem && item.step_key ? (
            <span className="sf-chapterplan-warn-actions">
              <button className="btn btn-quiet btn-xs" onClick={() => onGoToGateItem(item)}>
                {(item.primary_action && item.primary_action.label) || "去补这一步"}
              </button>
            </span>
          ) : null}
        </div>
      ))}
      {gateAdvisories.map((item, i) => (
        <div key={item.id || `gw${i}`} className="sf-chapterplan-warn tone-gold">
          <I.AlertTriangle size={12} /> {item.message}
        </div>
      ))}
    </div>
  );
}
