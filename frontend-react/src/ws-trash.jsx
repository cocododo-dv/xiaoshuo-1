import React from "react";
import { I } from "./icons.jsx";
import { agoLabel } from "./lib/ago.js";
import { useStoreTick } from "./lib/store-utils.js";
import { WsTrashStore } from "./ws-catalog.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { EmptyState, Notice, PageHeader, Spinner, Tag } from "./ws-ui.jsx";

/* ==========================================================
   回收站（WsTrashStore，按作品隔离）。
   以前挂在 ws-library.jsx 里：打开回收站要先下载整个资料库分块（图谱布局、档案数据层，
   后者一 import 就去拉 /library）。现在独立成路由模块，只依赖目录 store。
   场景的所在章也在回收站里时，场景行挂在那一章下面：恢复那一章会把它们一起带回，
   单独恢复这场会被后端拒绝，所以按钮禁用并写明原因。样式在 ws-library.css 的回收站一节。
   ========================================================== */

const { useMemo, useEffect } = React;

const TRASH_TYPE_BY_KIND = { 作品: "work", 章节: "chapter", 场景: "scene" };
const trashType = (it) => (it && it.payload && it.payload.type) || TRASH_TYPE_BY_KIND[it && it.kind] || String((it && it.id) || "").split(":")[0];
const trashChapterOf = (it) => (it && (it.chapterId || it.chapter_id)) || "";
const trashChapterKey = (it) => String(it.id).replace(/^chapter:/, "");

/* 表格行：章节后面紧跟随它一起回收的场景（nested），其余按 store 的顺序 */
function trashRows(items) {
  const chapterIds = new Set(items.filter(it => trashType(it) === "chapter").map(trashChapterKey));
  const childrenOf = {};
  const top = [];
  items.forEach((it) => {
    const cid = trashChapterOf(it);
    if (trashType(it) === "scene" && cid && chapterIds.has(cid)) (childrenOf[cid] = childrenOf[cid] || []).push(it);
    else top.push(it);
  });
  const rows = [];
  top.forEach((it) => {
    const kids = trashType(it) === "chapter" ? (childrenOf[trashChapterKey(it)] || []) : [];
    rows.push({ it, nested: false, childCount: kids.length });
    kids.forEach(kid => rows.push({ it: kid, nested: true, childCount: 0 }));
  });
  return rows;
}

function trashExactTime(ms) {
  try { return new Date(ms).toLocaleString("zh-CN", { hour12: false }); } catch (e) { return ""; }
}

const TRASH_PURGE_BODY = {
  work: "整部作品的章节、正文、构思与设定会一起删除，无法找回。",
  chapter: "这一章和其中的场景、正文会一起删除，无法找回。",
  scene: "这一场的正文与旁注会一起删除，无法找回。",
};
const TRASH_TONE = { work: "accent", chapter: "info" };

function TrashRow({ it, nested, childCount, onRestore, onPurge }) {
  const type = trashType(it);
  const blocked = it.restorable === false;
  const reason = blocked
    ? (nested ? "随本章一起移入回收站：恢复这一章会把它一起带回" : "所在章节也在回收站里：先恢复那一章")
    : "";
  return (
    <tr className={`trash-row is-${type} ${nested ? "is-nested" : ""}`}>
      <td><Tag tone={TRASH_TONE[type] || "neutral"}>{it.kind || "内容"}</Tag></td>
      <td className="trash-title-cell">
        <div className="trash-title">
          {nested && <span className="trash-branch" aria-hidden="true">└</span>}
          <span className="trash-title-text">{it.title}</span>
        </div>
        {type === "chapter" && childCount > 0 && <div className="trash-note">含 {childCount} 场，恢复本章会一起带回</div>}
        {blocked && <div className="trash-note">{nested ? "随本章一起移入" : reason}</div>}
      </td>
      <td className="trash-time" title={trashExactTime(it.removedAt || 0)}>{agoLabel(it.removedAt || 0)}</td>
      <td>
        <div className="trash-actions">
          {/* 读屏按 Tab 走下去听到的是一串同名按钮：名字里带上是哪一项（看得见的字不变） */}
          <button type="button" className="btn btn-ghost btn-sm" disabled={blocked} title={reason || undefined}
            aria-label={`恢复「${it.title}」`} onClick={() => onRestore(it)}>
            <I.Refresh size={13} /> 恢复
          </button>
          <button type="button" className="btn btn-danger btn-sm" aria-label={`永久删除「${it.title}」`} onClick={() => onPurge(it)}>永久删除</button>
        </div>
      </td>
    </tr>
  );
}

function WsTrash() {
  useStoreTick((fn) => (WsTrashStore ? WsTrashStore.subscribe(fn) : undefined));
  const items = WsTrashStore ? WsTrashStore.list() : [];
  const rows = useMemo(() => trashRows(items), [items]);
  /* 列表是空的有三种原因：真的空、还在读、读不到。只有第一种才说「回收站是空的」；
     读不到时只给一处错误和重试——删掉的东西不能看起来像是没了 */
  const load = (WsTrashStore && typeof WsTrashStore.loadState === "function" && WsTrashStore.loadState()) || { status: "ready" };

  /* 打开就刷新：整理章节时后端会自动把空章移进回收站，那条路径不经过前端的删除动作。
     store 还没有 refresh() 时退回到兼容壳 push()——它只重新拉一次回收站。
     不能广播 ws:trash-changed：那是「回收站内容变了」的信号，审阅队列和角标也在听，会白白多拉三次。 */
  useEffect(() => {
    if (!WsTrashStore) return;
    if (typeof WsTrashStore.refresh === "function") WsTrashStore.refresh();
    else if (typeof WsTrashStore.push === "function") WsTrashStore.push();
  }, []);

  const restore = (it) => { WsTrashStore.restore(it.id); };
  const purge = async (it) => {
    const ok = await wsConfirm({
      title: `永久删除「${it.title}」？`,
      body: TRASH_PURGE_BODY[trashType(it)] || "删除后无法找回。",
      confirmLabel: "永久删除",
      tone: "danger",
    });
    if (ok) WsTrashStore.purge(it.id);
  };
  const clearAll = async () => {
    if (!items.length) return;
    const ok = await wsConfirm({
      title: "清空回收站？",
      body: `这 ${items.length} 条内容会全部永久删除，无法找回。`,
      confirmLabel: "全部永久删除",
      tone: "danger",
    });
    if (ok) WsTrashStore.clear();
  };

  return (
    <div className="page trash-page" data-screen-label="trash">
      <div className="page-narrow">
        <PageHeader
          title="回收站"
          description="删掉的场景、章节和整部作品都先到这里，恢复后回到原来的位置。永久删除之后就找不回了。"
          actions={items.length > 0 ? <button type="button" className="btn btn-danger" onClick={clearAll}><I.Trash size={14} /> 清空回收站</button> : null}
        />
        {items.length === 0 && load.status === "error" ? (
          <Notice tone="danger" title="回收站没有读出来" testId="trash-load-error"
            actions={<button type="button" className="btn btn-ghost btn-sm" onClick={() => WsTrashStore.refresh()}>重试</button>}>
            {load.message}
          </Notice>
        ) : items.length === 0 && load.status === "loading" ? (
          <div className="card trash-card">
            <EmptyState compact title={<><Spinner size={13} /> 正在读取回收站…</>} />
          </div>
        ) : items.length === 0 ? (
          <div className="card trash-card">
            <EmptyState icon="Trash" title="回收站是空的">
              在写作台或章节编排里删掉的场景和章节、在书架里删掉的作品，都会先到这里，可以随时恢复。
            </EmptyState>
          </div>
        ) : (
          <div className="card trash-card">
            <table className="trash-table">
              <colgroup>
                <col className="trash-col-kind" />
                <col />
                <col className="trash-col-time" />
                <col className="trash-col-act" />
              </colgroup>
              <thead>
                <tr><th scope="col">类型</th><th scope="col">标题</th><th scope="col">移入时间</th><th scope="col"><span className="ws-sr-only">操作</span></th></tr>
              </thead>
              <tbody>
                {rows.map(({ it, nested, childCount }) => (
                  <TrashRow key={it.id} it={it} nested={nested} childCount={childCount} onRestore={restore} onPurge={purge} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export { WsTrash, trashRows };
