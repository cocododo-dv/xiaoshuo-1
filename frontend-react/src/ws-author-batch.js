import { wsConfirm } from "./ws-notify.jsx";
import { planDeleteNote } from "./ws-author-edits.js";

/* ==========================================================
   章节编排 · 批量删除（章 / 场两种多选，从 ws-author.jsx 拆出，2026-10）
   已批准终稿始终排除在选择之外：后端也会 blocked，这里先在 UI 上说清楚，
   免得作者勾了一批、只删掉一部分还得自己对账。不是 hook：每次渲染现拼一份。
   ctx：{ chapters, ch, chSel, scSel, pickedId, commitChapters, chaptersRef, updateCurrent, chName, noticeTrashed,
          pickChapter（删掉的章里有正在看的那一章时落到哪一章）, clearHighlight }
   ========================================================== */

export function arrBatches({
  chapters, ch, chSel, scSel, pickedId, commitChapters, chaptersRef, updateCurrent, chName, noticeTrashed, pickChapter, clearHighlight,
}) {
  const locked = ch.state === "approved";
  const selectableChapters = chapters.filter((c) => c.state !== "approved");
  const selectedChapters = selectableChapters.filter((c) => chSel.has(c.id));
  const chapterBatch = {
    mode: chSel.mode,
    has: chSel.has,
    onToggle: chSel.toggle,
    onEnter: chSel.enter,
    onExit: chSel.exit,
    selectedCount: selectedChapters.length,
    selectableCount: selectableChapters.length,
    allSelected: !!selectableChapters.length && selectedChapters.length === selectableChapters.length,
    onSelectAll: () => chSel.toggleAll(selectableChapters.map((c) => c.id)),
    onDelete: async () => {
      const targets = selectedChapters;
      if (!targets.length) return;
      const scenes = targets.reduce((n, c) => n + (c.scenes || []).length, 0);
      const what = targets.length === 1
        ? chName(targets[0])
        : `所选 ${targets.length} 章`;
      const ok = await wsConfirm({
        title: `把${what}移入回收站？`,
        body: `${scenes ? `连同章下 ${scenes} 个场景。` : ""}可以在回收站里恢复。${planDeleteNote(targets)}`,
        confirmLabel: "移入回收站",
        tone: "danger",
      });
      if (!ok) return;
      const drop = new Set(targets.map((c) => c.id));
      const survivor = chaptersRef.current.find((c) => !drop.has(c.id));
      commitChapters((cs) => cs.filter((c) => !drop.has(c.id)));
      chSel.exit();
      if (drop.has(pickedId)) pickChapter(survivor ? survivor.id : null);
      noticeTrashed(`已把 ${targets.length} 章移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
    },
  };
  const sceneBatch = {
    mode: scSel.mode,
    has: scSel.has,
    onToggle: scSel.toggle,
    onToggleMode: scSel.toggleMode,
    onSelectAll: () => scSel.toggleAll(ch.scenes.map((s) => s.sid)),
    onDelete: async () => {
      if (locked) return;
      const targets = ch.scenes.filter((s) => scSel.has(s.sid));
      if (!targets.length) return;
      const what = targets.length === 1 ? `场景「${targets[0].title}」` : `本章 ${targets.length} 个场景`;
      const ok = await wsConfirm({ title: `把${what}移入回收站？`, body: "可以在回收站里恢复。", confirmLabel: "移入回收站", tone: "danger" });
      if (!ok) return;
      const drop = new Set(targets.map((s) => s.sid));
      updateCurrent((c) => ({ ...c, scenes: c.scenes.filter((s) => !drop.has(s.sid)) }));
      scSel.exit();
      clearHighlight();
      noticeTrashed(`已把 ${targets.length} 个场景移入回收站`);
    },
  };
  return { chapterBatch, sceneBatch };
}
