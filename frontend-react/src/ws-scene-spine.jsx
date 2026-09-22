import React from "react";
import { I } from "./icons.jsx";
import { IconButton, Segmented, Tag } from "./ws-ui.jsx";
import { stateLabelOf, stateToneOf } from "./ws-scene-derive.js";
import { SCENE_STATE_META, chapterLabel, chapterOwnTitle, sceneLabel, sceneNoLabel } from "./ws-labels.js";

const { useState } = React;

/* ==========================================================
   AI 起草台 — 左栏：全书书脊（章 → 场）
   与写作台大纲、章节编排读同一份目录。在办的场（交给 AI 的、跑过管线的）带着管线状态和「移出」；
   其余的场按目录状态显示，点一下就放上台面。
   在办的行沿用原队列的测试标识（scene-queue-item / scene-queue-remove），其余的行是 scene-spine-item。
   ========================================================== */

/* 书脊上一场的状态词：按目录状态标的场、跑完归档的场，都用 ws-labels 的同一份（写作台大纲、
   章节编排、成稿中心、主页也读它）——同一场写完了，不再这里叫「已归档」、那里叫「已完成」。
   在办场的管线中间态（运行中 / 待起草 / 待复核）是起草台自己的，照旧；交给了 AI、还没有运行记录的
   没写的场，和排着队的场一样叫「待起草」。 */
function spineChip({ fromRun, pinned, st, jobQueued }) {
  if (fromRun && st !== "archived") return { label: stateLabelOf(st, jobQueued), tone: stateToneOf(st, jobQueued) };
  if (!fromRun && pinned && st === "todo") return { label: stateLabelOf("queued"), tone: stateToneOf("queued") };
  const m = SCENE_STATE_META[fromRun ? "done" : st] || SCENE_STATE_META.todo;
  return { label: m.label, tone: m.tone };
}

/* filter：all = 全书 · active = 只看在办 · review = 只看待复核。
   select：多选移出的会话态（useSceneQueue 给出）。pendingSync(backendId)：构思更新了、场景卡还没跟上。 */
function SceneSpine({ chapters, queue, pickedId, counts, filter, setFilter, pendingSync, onPickItem, onViewScene, onEnqueueChapter, onRemove, select }) {
  const sel = select || {};
  const selectMode = !!sel.mode;
  const selectedCount = sel.count || 0;
  const itemBySid = {};
  queue.forEach(q => { itemBySid[q.sid] = q; });
  const pinnedCount = queue.filter(q => !q.transient).length;
  const pinnedOnly = filter === "active" || filter === "review" || selectMode;
  const showScene = (s) => {
    const item = itemBySid[s.sid];
    const pinned = !!(item && !item.transient);
    if (selectMode || filter === "active") return pinned;
    if (filter === "review") return pinned && item.state === "ready";
    return true;
  };
  const pickedSid = (queue.find(q => q.id === pickedId) || {}).sid || "";
  const totalScenes = chapters.reduce((n, c) => n + (c.scenes || []).length, 0);
  /* 章的展开态：书不大就全展开；大了只展开当前章、落点所在章和有在办场的章。作者手动开合的优先。 */
  const [openMap, setOpenMap] = useState({});
  const isOpen = (c) => {
    if (openMap[c.id] != null) return openMap[c.id];
    if (pinnedOnly || totalScenes <= 40) return true;
    return !!c.current || (c.scenes || []).some(s => s.sid === pickedSid || (itemBySid[s.sid] && !itemBySid[s.sid].transient));
  };
  const rows = chapters
    .map(c => ({ c, scenes: (c.scenes || []).filter(showScene) }))
    .filter(g => g.scenes.length);
  const emptyText = filter === "review"
    ? "没有等你复核的稿。"
    : (pinnedOnly ? "还没有在办的场——在「全书」里点一场开始起草，或用章标题右边的「整章入列」。" : "目录里还没有场景。");
  return (
    <aside className="scn2-queue" data-testid="scene-spine" aria-label="全书场景">
      <header className="scn2-spine-head">
        <div className="scn2-spine-titlerow">
          <h2 className="scn2-spine-title text-serif">全书场景</h2>
          {!selectMode && (
            <button type="button" className="btn btn-quiet btn-xs scn2-spine-select" data-testid="scene-queue-select-mode"
              disabled={!pinnedCount} title="多选在办的场，一次移出多场" onClick={sel.onToggleMode}>
              多选
            </button>
          )}
        </div>
        {!selectMode ? (
          <Segmented
            block size="sm" label="书脊筛选" value={filter} onChange={setFilter} className="scn2-spine-filter"
            options={[
              { value: "all", label: "全书", count: totalScenes, testId: "scene-spine-filter-all" },
              { value: "active", label: "在办", count: pinnedCount, testId: "scene-spine-filter-active" },
              { value: "review", label: "待复核", count: counts.ready, testId: "scene-spine-filter-review" },
            ]}
          />
        ) : (
          <div className="scn2-queue-batch" role="toolbar" aria-label="在办批量操作">
            <div className="scn2-queue-batch-top">
              <span className="scn2-queue-batch-n">已选 <strong>{selectedCount}</strong> / {sel.total || 0}</span>
              <button className="btn btn-quiet btn-xs" onClick={sel.onSelectAll}>
                {selectedCount === (sel.total || 0) && sel.total ? "取消全选" : "全选"}
              </button>
            </div>
            <div className="scn2-queue-batch-top">
              <button className="btn btn-danger btn-sm scn2-queue-batch-go" data-testid="scene-queue-batch-remove"
                disabled={!selectedCount} onClick={sel.onRemoveSelected}>
                <I.Trash size={12} /> 移出所选
              </button>
              <button className="btn btn-ghost btn-sm" data-testid="scene-queue-select-exit" onClick={sel.onToggleMode}>完成</button>
            </div>
          </div>
        )}
        {counts.running > 0 && (
          <p className="scn2-spine-live"><span className="scn2-chip-pulse" aria-hidden="true" />{counts.running} 场正在运行</p>
        )}
      </header>

      <div className="scn2-queue-list">
        {!rows.length && <p className="scn2-spine-empty">{emptyText}</p>}
        {rows.map(({ c, scenes }) => {
          const all = c.scenes || [];
          const finished = all.filter(s => s.state === "done" || (itemBySid[s.sid] && itemBySid[s.sid].state === "archived")).length;
          const batch = all.filter(s => s.state !== "done" && !(itemBySid[s.sid] && !itemBySid[s.sid].transient)).map(s => s.sid);
          const open = isOpen(c);
          return (
            <section key={c.id} className="scn2-spine-ch" data-testid="scene-spine-chapter" data-chapter-id={c.id}>
              <header className="scn2-spine-chhead">
                <button className="scn2-spine-chbtn" aria-expanded={open}
                  onClick={() => setOpenMap(m => ({ ...m, [c.id]: !open }))} title={c.summary || c.title}>
                  <span className={`scn2-spine-chev${open ? " is-open" : ""}`}><I.ChevronRight size={12} /></span>
                  {/* 章号只在有真章名时作小字；占位名（第 N 章 / 未命名）不和章号并排 */}
                  {chapterOwnTitle(c) && <span className="scn2-spine-chn tab-num">{chapterLabel(c, { withTitle: false })}</span>}
                  <span className="scn2-spine-cht text-serif">{chapterOwnTitle(c) || chapterLabel(c, { withTitle: false })}</span>
                  {c.spine && <span className="scn2-spine-mark">{c.spine}</span>}
                  <span className="scn2-spine-chp tab-num" title={`已完成 ${finished} / ${all.length} 场`}>{finished}/{all.length}</span>
                </button>
                {!selectMode && !pinnedOnly && batch.length > 0 && (
                  <IconButton icon="Plus" size="xs" className="scn2-spine-chall" testId="scene-spine-enqueue-chapter"
                    label={`整章入列：把${chapterLabel(c, { withTitle: false })}还没写完的 ${batch.length} 场交给 AI（不自动起草，逐场点「开始起草」）`}
                    onClick={() => onEnqueueChapter && onEnqueueChapter(batch)} />
                )}
              </header>
              {open && (
                <ul className="scn2-spine-scenes">
                  {scenes.map((s) => {
                    const index = all.indexOf(s);
                    const item = itemBySid[s.sid];
                    const pinned = !!(item && !item.transient);
                    /* 在办但还没有运行记录（后端恢复是逐场进行的）：先按目录状态标，不写一句「待起草」的假话 */
                    const fromRun = pinned && (!!item.hasRun || (item.state || "queued") !== "queued");
                    const st = fromRun ? (item.state || "queued") : (SCENE_STATE_META[s.state] ? s.state : "todo");
                    const active = !!item && pickedId === item.id;
                    const checked = pinned && !!(sel.has && sel.has(item.id));
                    // 章已经在分组标题里：行上只写「第 3 场」，完整坐标给读屏
                    const label = sceneNoLabel(index);
                    const stale = pendingSync && s.backendId ? pendingSync(s.backendId) : false;
                    const { label: chipLabel, tone: chipTone } = spineChip({ fromRun, pinned, st, jobQueued: item && item.jobQueued });
                    return (
                      <li key={s.sid} className={`scn2-qrow-wrap ${checked ? "is-selected" : ""}`}>
                        {selectMode && pinned && (
                          <label className="scn2-qrow-check" title="选中后可批量移出在办清单">
                            <input type="checkbox" checked={checked} aria-label={`选择${sceneLabel(c, index)}「${s.title}」`} onChange={() => sel.onToggle(item.id)} />
                          </label>
                        )}
                        <button className={`scn2-qrow ${active ? "is-active" : ""} ${pinned ? "is-pinned s-" + st : "is-idle"}`}
                          data-testid={pinned ? "scene-queue-item" : "scene-spine-item"} data-scene-sid={s.sid || ""}
                          aria-current={active ? "true" : undefined}
                          title={s.summary || s.title}
                          onClick={() => {
                            if (selectMode) { if (pinned) sel.onToggle(item.id); return; }
                            if (item) onPickItem(item.id); else onViewScene(s.sid);
                          }}>
                          <span className="scn2-qrow-num tab-num">{label}</span>
                          <span className="scn2-qrow-title text-serif">{s.title}</span>
                          {stale && <span className="scn2-qrow-sync" title="构思里这一场已经更新，场景卡还没同步"><I.Refresh size={11} /><span className="ws-sr-only">构思已更新</span></span>}
                          {/* 目录里还没动过的场不挂「待起草」：满屏同一个标签只是噪音，有状态的场才标 */}
                          {(pinned || st !== "todo") && (
                            <Tag tone={chipTone} className={`scn2-chip s-${st}`}>
                              {pinned && st === "running" && <span className="scn2-chip-pulse" aria-hidden="true" />}
                              {chipLabel}
                            </Tag>
                          )}
                        </button>
                        {!selectMode && pinned && (
                          <button className="scn2-qrow-x" data-testid="scene-queue-remove" aria-label={`把 ${s.title} 移出在办`}
                            title="移出在办清单（保留场景卡与已生成的 AI 稿，可撤销）"
                            onClick={(e) => { e.stopPropagation(); if (onRemove) onRemove(item.id); }}><I.X size={12} /></button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </aside>
  );
}

export { SceneSpine, spineChip };
