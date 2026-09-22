import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog, WsTrashStore } from "./ws-catalog.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { CloseButton, Tag } from "./ws-ui.jsx";
import { chapterHeading, chapterLabel, chapterStateMeta, sceneLabel, sceneStateMeta } from "./ws-labels.js";
import { isImeComposing } from "./ws-dialog.jsx";
import { useWrEvent, useWrInert } from "./ws-writer-hooks.js";

/* ==========================================================
   章节大纲（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   · WrOutline / WrChapter：左栏的章与场。React.memo——敲字、每次自动保存后的目录通知
     都不再让整本书的大纲跟着重渲染（大纲形状没变时 useWrCatalog 沿用同一份数组）。
   · useWrOutlineActions：改名、重排、增删、批量删除。全部写穿 WsCatalog（单一真相源），
     回调身份稳定，memo 才有意义。
   ESM 模块，不写 window。
   ========================================================== */

const { memo, useCallback, useEffect, useRef, useState } = React;

function WrOutlineImpl({ open, activeScene, chapters, onReorder, onRename, onDelete, onDeleteChapter, onDeleteBatch, onAdd, onPick, onClose }) {
  const list = chapters || [];
  const allScenes = list.flatMap(c => c.scenes);
  const doneCount = allScenes.filter(s => s.state === "done").length;
  const pct = allScenes.length ? Math.round((doneCount / allScenes.length) * 100) : 0;
  /* 多选态只活在抽屉里（不落 localStorage）：退出多选或删除完成即清空——
     勾选是瞬时意图，跨会话留着只会让作者对着一份看不见来路的勾选下手 */
  const [selectMode, setSelectMode] = useState(false);
  const [selCh, setSelCh] = useState(() => new Set());
  const [selSc, setSelSc] = useState(() => new Set());
  const openable = list.filter(c => c.state !== "approved");
  const selectableScenes = openable.flatMap(c => c.scenes);
  /* 勾了整章就等于勾了它名下所有场：单独再算一遍会把「3 章 + 7 场」这种
     数字报成作者根本没做过的选择，删除范围也跟着看不懂。这里只统计
     「不在已选章名下的散场」，并让那些场在行上显示为随章带走。 */
  const scenesUnderSelectedChapters = new Set(
    list.filter(c => selCh.has(c.id)).flatMap(c => c.scenes.map(s => s.id))
  );
  const looseScenes = [...selSc].filter(id => !scenesUnderSelectedChapters.has(id));
  const selectedCount = selCh.size + looseScenes.length;
  const exitSelect = () => { setSelectMode(false); setSelCh(new Set()); setSelSc(new Set()); };
  const toggleCh = useCallback((id) => setSelCh((prev) => toggleIn(prev, id)), []);
  const toggleSc = useCallback((id) => setSelSc((prev) => toggleIn(prev, id)), []);
  const allSelected = !!(openable.length || selectableScenes.length)
    && selCh.size === openable.length && selSc.size === selectableScenes.length;
  const asideRef = useRef(null);
  useWrInert(asideRef, !open);
  return (
    <aside ref={asideRef} className={`wr-drawer left ${open ? "show" : ""}`} aria-label="章节大纲">
      <header className="wr-drawer-head">
        <I.BookOpen size={16} /><span className="wr-drawer-title">{selectMode ? "选择要删除的内容" : "章节大纲"}</span>
        <button type="button" className={`wr-drawer-sel ${selectMode ? "is-on" : ""}`} title={selectMode ? "退出多选" : "多选章节 / 场景后可批量删除"}
          data-testid="writer-outline-select-mode" aria-pressed={selectMode}
          onClick={() => (selectMode ? exitSelect() : setSelectMode(true))}>
          <I.Check size={13} />{selectMode ? "完成" : "多选"}
        </button>
        <CloseButton className="wr-drawer-x" label="收起大纲" onClick={onClose} />
      </header>
      <div className="wr-drawer-body">
        <div className="wr-rail-progress">
          <div className="wr-rail-progress-top">
            <span className="wr-rail-progress-k">全书进度</span>
            <span className="wr-rail-progress-v">{doneCount}/{allScenes.length} 场 · {pct}%</span>
          </div>
          <div className="wr-rail-progress-bar"><div className="wr-rail-progress-fill" style={{ width: pct + "%" }} /></div>
        </div>
        {/* 每章只拿自己需要的东西：落点不在这一章时 activeScene 传 null，不在多选时勾选集合传同一个空集——
            换场、敲字都只重渲染涉及的那一两章 */}
        {list.map(c => (
          <WrChapter key={c.id} ch={c} activeScene={c.scenes.some(s => s.id === activeScene) ? activeScene : null}
            onPick={onPick} onReorder={onReorder} onRename={onRename}
            onDelete={onDelete} onDeleteChapter={onDeleteChapter} onAdd={onAdd}
            selectMode={selectMode} chChecked={selCh.has(c.id)}
            checkedScenes={selectMode ? selSc : NO_SELECTION}
            scFollowsChapter={selCh.has(c.id)}
            onToggleCh={toggleCh} onToggleSc={toggleSc} />
        ))}
      </div>
      {selectMode && (
        <footer className="wr-outline-batch" role="toolbar" aria-label="大纲批量操作">
          <span className="wr-outline-batch-n">
            {selectedCount
              ? <>已选 <strong>{selCh.size}</strong> 章、<strong>{looseScenes.length}</strong> 场{scenesUnderSelectedChapters.size ? `（另含随章带走 ${scenesUnderSelectedChapters.size} 场）` : ""}</>
              : "勾选章或场；点场景行也可选中"}
          </span>
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => {
            if (allSelected) { setSelCh(new Set()); setSelSc(new Set()); return; }
            setSelCh(new Set(openable.map(c => c.id)));
            setSelSc(new Set(selectableScenes.map(s => s.id)));
          }}>{allSelected ? "取消全选" : "全选"}</button>
          <button type="button" className="btn btn-danger btn-sm" data-testid="writer-outline-batch-delete" disabled={!selectedCount}
            onClick={() => { onDeleteBatch && onDeleteBatch([...selCh], [...selSc]); exitSelect(); }}>
            <I.Trash size={13} /> 删除所选
          </button>
        </footer>
      )}
    </aside>
  );
}

const NO_SELECTION = new Set();
function toggleIn(set, id) {
  const next = new Set(set);
  if (next.has(id)) next.delete(id); else next.add(id);
  return next;
}


function WrChapterImpl({ ch, activeScene, onPick, onReorder, onRename, onDelete, onDeleteChapter, onAdd, selectMode, chChecked, checkedScenes, scFollowsChapter, onToggleCh, onToggleSc }) {
  const scChecked = (id) => scFollowsChapter || checkedScenes.has(id);
  const [open, setOpen] = useState(ch.expanded || ch.scenes.some(s => s.id === activeScene));
  const [over, setOver] = useState(null);
  const [editing, setEditing] = useState(null);
  const [editVal, setEditVal] = useState("");
  const dragFrom = useRef(null);
  const listRef = useRef(null);
  /* 章徽标：阶段与叫法都是 ws-labels 的同一份（成稿中心、主页、章节编排也读它）。以前这里自带一份词表，
     还照抄目录状态——写了四千字的章在大纲里挂着蓝色的「规划」，别处都说「写作中」 */
  const stageMeta = chapterStateMeta(ch.stage || ch.state);
  const head = chapterHeading(ch);
  const holdsActive = ch.scenes.some(s => s.id === activeScene);
  /* 落点换到这一章（上一场 / 下一场、深链、从别的台子跳过来）时自动展开 */
  useEffect(() => { if (holdsActive) setOpen(true); }, [holdsActive]);
  const locked = ch.state === "approved";
  const startEdit = (s) => { if (!locked) { setEditing(s.id); setEditVal(s.title); } };
  const commit = () => { if (editing && onRename) onRename(ch.id, editing, editVal.trim() || "未命名"); setEditing(null); };
  /* 用回车 / Esc 结束改名时，焦点回到这一场的行按钮（输入框一卸载，焦点就掉到 <body> 上）；
     点到别处（失焦提交）时焦点已经在别处，不抢 */
  const refocusRef = useRef(null);
  const endEditByKey = (save) => {
    refocusRef.current = editing;
    if (save) commit(); else setEditing(null);
  };
  useEffect(() => {
    const sid = refocusRef.current;
    if (editing !== null || sid == null) return;
    refocusRef.current = null;
    const btn = listRef.current && [...listRef.current.querySelectorAll("[data-scene-open]")].find(node => node.getAttribute("data-scene-open") === sid);
    if (btn) btn.focus();
  }, [editing]);
  /* 键盘重排：Alt + ↑ / ↓ 移动手加的场（雪花整理出来的场先后在构思第 9 步定，这里不动）。
     移动后把焦点放回同一场，连按几下就能挪几格。 */
  const moveByKey = (i, delta) => {
    const s = ch.scenes[i];
    const to = i + delta;
    if (!s || s.planOwned || locked || to < 0 || to >= ch.scenes.length || !onReorder) return;
    onReorder(ch.id, i, to);
    requestAnimationFrame(() => {
      const btn = listRef.current && [...listRef.current.querySelectorAll("[data-scene-open]")].find(node => node.getAttribute("data-scene-open") === s.id);
      if (btn) btn.focus();
    });
  };
  return (
    <div className="wr-ch">
      <div className={`wr-ch-head ${chChecked ? "is-selected" : ""}`}>
        {selectMode && (
          <label className="wr-ch-check" title={locked ? "已批准终稿不可删除——请先在成稿中心重新打开" : "选中本章（含章下场景）"}>
            <input type="checkbox" checked={!!chChecked} disabled={locked} aria-label={`选择${chapterLabel(ch)}`}
              onChange={() => onToggleCh && onToggleCh(ch.id)} />
          </label>
        )}
        <button type="button" className="wr-ch-row" aria-expanded={open} onClick={() => setOpen(v => !v)}>
          <span className="wr-ch-chev" style={{ transform: open ? "rotate(90deg)" : "none" }}><I.ChevronRight size={13} /></span>
          {/* 章号「第 N 章」只在有真章名时作小字放在旁边；章名还是占位（第 N 章 / 未命名）时只写一遍 */}
          {head.title && <span className="wr-ch-num">{head.num}</span>}
          {/* 大纲栏很窄：脊柱标记与章摘要放进提示，不再多挤一枚徽标把章名压成省略号 */}
          <span className="wr-ch-title" title={[ch.spine ? `收在${ch.spine}` : "", ch.summary || ch.title].filter(Boolean).join(" · ")}>{head.title || head.num}</span>
          <Tag tone={stageMeta.tone} dot className="wr-ch-pill">{stageMeta.label}</Tag>
        </button>
        {!selectMode && (
          <button type="button" className="wr-sc-actbtn danger wr-ch-del" disabled={locked}
            title={locked ? "终稿已锁定" : "删除本章（含章下场景）"} aria-label={locked ? "终稿已锁定，不能删除" : `删除${head.num}`}
            onClick={(e) => { e.stopPropagation(); onDeleteChapter && onDeleteChapter(ch.id); }}><I.Trash size={12} /></button>
        )}
      </div>
      {open && (
        <ul className="wr-sc-list" ref={listRef}>
          {ch.scenes.map((s, i) => {
            const active = activeScene === s.id;
            const checked = !!(selectMode && scChecked(s.id));
            const reorderable = !locked && !s.planOwned;
            /* 怎么调先后：把手上有悬停提示，读屏与键盘用户从行按钮的描述里听到同一句 */
            const orderHint = locked ? "终稿已锁定" : s.planOwned ? "雪花整理出来的场：先后在构思第 9 步「场景列表」里拖动，确认后自动同步到这里" : "拖动或 Alt + ↑ / ↓ 调整先后";
            const hintId = "wr-sc-hint-" + String(s.id).replace(/[^A-Za-z0-9_-]/g, "_");
            return (
              <li key={s.id}
                draggable={!locked && !selectMode && editing !== s.id && !s.planOwned}
                onDragStart={(e) => { if (s.planOwned) { e.preventDefault(); return; } dragFrom.current = i; if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; }}
                onDragOver={(e) => { e.preventDefault(); if (over !== i) setOver(i); }}
                onDrop={(e) => { e.preventDefault(); const from = dragFrom.current; if (from != null && from !== i && onReorder) onReorder(ch.id, from, i); dragFrom.current = null; setOver(null); }}
                onDragEnd={() => { dragFrom.current = null; setOver(null); }}>
                <div className={`wr-sc ${active ? "is-active" : ""} ${over === i ? "is-over" : ""} ${editing === s.id ? "is-editing" : ""} ${checked ? "is-selected" : ""}`}>
                  {selectMode ? (
                    <label className="wr-sc-check"
                      title={scFollowsChapter ? "本章已选中，这一场随章一起删除" : undefined}>
                      <input type="checkbox" checked={scChecked(s.id)} disabled={locked || scFollowsChapter}
                        aria-label={`选择场景 ${s.title}`} onChange={() => onToggleSc && onToggleSc(s.id)} />
                    </label>
                  ) : (
                    <span className={`wr-sc-grip ${s.planOwned ? "is-fixed" : ""}`} aria-hidden="true"
                      title={orderHint}><I.GripVertical size={13} /></span>
                  )}
                  {!selectMode && <span id={hintId} className="ws-sr-only">{orderHint}</span>}
                  {editing === s.id ? (
                    <input className="wr-sc-edit" autoFocus value={editVal} aria-label="场景名"
                      onChange={(e) => setEditVal(e.target.value)}
                      onKeyDown={(e) => {
                        if (isImeComposing(e)) return; // 输入法确认候选的那一下回车不是「改完了」
                        if (e.key === "Enter") { e.preventDefault(); endEditByKey(true); } else if (e.key === "Escape") { e.preventDefault(); endEditByKey(false); }
                      }}
                      onBlur={commit} />
                  ) : (
                    <button type="button" className="wr-sc-open" data-scene-open={s.id}
                      aria-current={active ? "true" : undefined}
                      aria-label={`${s.title}（${sceneStateMeta(s.state).label}）`}
                      aria-describedby={selectMode ? undefined : hintId}
                      aria-keyshortcuts={!selectMode && reorderable ? "Alt+ArrowUp Alt+ArrowDown" : undefined}
                      aria-pressed={selectMode ? checked : undefined}
                      onClick={() => {
                        if (selectMode) { if (!locked && !scFollowsChapter) onToggleSc && onToggleSc(s.id); return; }
                        onPick(s.id);
                      }}
                      onKeyDown={(e) => {
                        if (!e.altKey || selectMode || !reorderable) return;
                        if (e.key === "ArrowUp") { e.preventDefault(); moveByKey(i, -1); }
                        else if (e.key === "ArrowDown") { e.preventDefault(); moveByKey(i, 1); }
                      }}
                      onDoubleClick={(e) => { e.stopPropagation(); if (!selectMode) startEdit(s); }}>
                      <span className={`wr-sc-mark s-${s.state}`} aria-hidden="true">
                        {s.state === "done" && <I.Check size={11} />}
                        {s.state === "active" && <span className="wr-sc-dot" />}
                        {s.state === "todo" && <I.Circle size={11} />}
                      </span>
                      <span className="wr-sc-name" title={s.summary || s.title}>{s.title}</span>
                    </button>
                  )}
                  {editing !== s.id && !selectMode && (
                    <span className="wr-sc-act">
                      <button type="button" className="wr-sc-actbtn" disabled={locked} title={locked ? "终稿已锁定" : "重命名"} aria-label={`重命名 ${s.title}`}
                        onClick={(e) => { e.stopPropagation(); startEdit(s); }}><I.Edit size={12} /></button>
                      <button type="button" className="wr-sc-actbtn danger" disabled={locked} title={locked ? "终稿已锁定" : "删除场景"} aria-label={`删除 ${s.title}`}
                        onClick={(e) => { e.stopPropagation(); onDelete && onDelete(ch.id, s.id); }}><I.Trash size={12} /></button>
                    </span>
                  )}
                </div>
              </li>
            );
          })}
          {!selectMode && <li className="wr-sc-add"><button type="button" className="wr-sc-addbtn" disabled={locked} title={locked ? "终稿已锁定" : undefined} onClick={() => onAdd && onAdd(ch.id)}><I.Plus size={12} /> 添加场景</button></li>}
        </ul>
      )}
    </div>
  );
}

const WrOutline = memo(WrOutlineImpl);
const WrChapter = memo(WrChapterImpl);

/* 大纲的结构操作。refresh：从目录重读大纲；onRemoved(ids)：删掉的场里有当前这一场时换落点；
   showNotice：本视图的回执条（删完一片安静是最容易让人以为「丢了」的时刻） */
export function useWrOutlineActions({ chapters, refresh, activeScene, setActiveScene, showNotice, go }) {
  const chapterLocked = (chId) => chapters.some((chapter) => chapter.id === chId && chapter.state === "approved");

  /* 删除后把光标放到还活着的第一场（正文文档留在存储里，随回收站恢复一并回来），
     并给一条回执说明东西去了哪 */
  const settleAfterDelete = (removedSceneIds, noticeText) => {
    const next = refresh();
    if (removedSceneIds.has(activeScene)) {
      const flat = next.flatMap((chapter) => chapter.scenes);
      setActiveScene(flat.length ? flat[0].id : null);
    }
    if (noticeText) {
      showNotice({
        text: noticeText,
        actionLabel: "打开回收站",
        onAction: () => { if (go) go("trash"); else window.location.hash = "#trash"; },
      });
    }
  };

  const onReorder = useWrEvent((chId, from, to) => {
    if (chapterLocked(chId) || !WsCatalog) return;
    WsCatalog.moveScene(chId, from, to);
    refresh();
  });
  const onRename = useWrEvent((chId, sid, title) => {
    if (chapterLocked(chId) || !WsCatalog) return;
    WsCatalog.renameScene(chId, sid, title);
    refresh();
  });
  const onAdd = useWrEvent((chId) => {
    if (chapterLocked(chId) || !WsCatalog) return;
    WsCatalog.addScene(chId, "新场景");
    refresh();
  });
  const onDelete = useWrEvent((chId, sid) => {
    if (chapterLocked(chId) || !WsCatalog) return;
    // 进回收站（正文文档保留在存储里，恢复时一并回来；彻底删除时由回收站清理）
    const hit = WsCatalog.sceneById(sid);
    if (hit && WsTrashStore) {
      WsTrashStore.push({
        kind: "场景",
        title: sceneLabel(hit.chapter, hit.index, hit.scene, { withTitle: true, maxTitle: 40 }),
        payload: { type: "scene", chId: hit.chapter.id, index: hit.index, scene: hit.scene },
      });
    }
    const title = (hit && hit.scene.title) || "未命名场景";
    WsCatalog.removeScene(chId, sid);
    settleAfterDelete(new Set([sid]), `已把场景「${title}」移入回收站`);
  });
  const onDeleteChapter = useWrEvent(async (chId) => {
    if (!WsCatalog || chapterLocked(chId)) return;
    const target = chapters.find((chapter) => chapter.id === chId);
    if (!target) return;
    const scenes = target.scenes.length;
    const ok = await wsConfirm({
      title: `把第 ${target.n} 章「${target.title}」移入回收站？`,
      body: scenes ? `章下 ${scenes} 个场景和它们的正文一起进回收站，可以在回收站里恢复。` : "可以在回收站里恢复。",
      confirmLabel: "移入回收站",
      tone: "danger",
    });
    if (!ok) return;
    WsCatalog.removeChapters([chId]);
    settleAfterDelete(new Set(target.scenes.map((scene) => scene.id)), `已把第 ${target.n} 章移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
  });
  /* 批量删除：章 + 场一次提交（被删章下的场不再单独进场景桶，
     否则后端会以「章下已有单独回收的场景」为由挡下整章删除） */
  const onDeleteBatch = useWrEvent(async (chapterIds, sceneIds) => {
    if (!WsCatalog) return;
    const chDrop = new Set(chapterIds.filter((id) => !chapterLocked(id)));
    const scDrop = new Set();
    const removedScenes = new Set();
    chapters.forEach((chapter) => {
      chapter.scenes.forEach((scene) => {
        if (chDrop.has(chapter.id)) { removedScenes.add(scene.id); return; }
        if (sceneIds.includes(scene.id) && chapter.state !== "approved") { scDrop.add(scene.id); removedScenes.add(scene.id); }
      });
    });
    if (!chDrop.size && !scDrop.size) return;
    const parts = [];
    if (chDrop.size) parts.push(`${chDrop.size} 章`);
    if (scDrop.size) parts.push(`${scDrop.size} 场`);
    const ok = await wsConfirm({
      title: `把所选 ${parts.join("、")}移入回收站？`,
      body: `共 ${removedScenes.size} 个场景和它们的正文一起进回收站，可以在回收站里恢复。`,
      confirmLabel: "移入回收站",
      tone: "danger",
    });
    if (!ok) return;
    WsCatalog.removeMixed([...chDrop], [...scDrop]);
    settleAfterDelete(removedScenes, `已把 ${parts.join("、")}移入回收站`);
  });
  /* 空白作品：创建第一章 + 开场，立刻可写 */
  const createFirstChapter = useWrEvent(() => {
    if (!WsCatalog) return;
    WsCatalog.addChapter();
    refresh();
    const hit = WsCatalog.writingScene();
    if (hit) setActiveScene(hit.scene.sid);
  });

  return { onReorder, onRename, onAdd, onDelete, onDeleteChapter, onDeleteBatch, createFirstChapter };
}

export { WrOutline };
