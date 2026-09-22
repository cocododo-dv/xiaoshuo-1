import React from "react";
import { WsCatalog } from "./ws-catalog.jsx";
import { wsKey } from "./ws-works.jsx";
import { navigateWithViewIntent, queueViewIntent } from "./ws-view-intents.js";
import { wsConfirm } from "./ws-notify.jsx";
import { ARR_ACTS } from "./ws-author-data.jsx";
import { arrIsPlanChapter, arrIsPlanScene } from "./ws-author-derive.js";

/* ==========================================================
   章节编排 · 外壳的管线（hooks + 两个小工具）
   ----------------------------------------------------------
   WsAuthor 只管「现在看哪一章、哪种模式」和各块怎么拼；下面这些是它的零件：
   · useArrPref —— 按作品落地的界面偏好（只有 arr.mode / arr.lens / arr.picked 三个键）
   · useArrChapterList —— 本页的章表：拖拽中的即时视图 + 写回目录（服务端目录始终是单一真相源）
   · useSelection —— 章 / 场两种多选共用的一套 Set 选择
   · useAuthorSnow —— 构思 → 目录的回流（SnowSync.resync）与「整理章节结构」这扇门给不给
   · useChapterDnd / useSceneDnd —— 章 / 场的拖动与方向键挪位
   · arrGoView —— 跨视图跳转的唯一出口（有宿主的 go 就走 go，没有就排队意图 + 改 hash）
   不写 window（只读 window.SnowSync）；不 import 任何 ws-author 视图模块，免得成环。
   ========================================================== */

const { useState, useRef, useEffect, useMemo, useCallback } = React;

/* ---- 界面偏好 ---- */
const prefGet = (k, d) => { try { const v = localStorage.getItem(wsKey(k)); return v == null ? d : JSON.parse(v); } catch (_) { return d; } };
const prefSet = (k, v) => { try { localStorage.setItem(wsKey(k), JSON.stringify(v)); } catch (_) {} };

export function useArrPref(key, fallback) {
  const [value, setValue] = useState(() => prefGet(key, fallback));
  useEffect(() => { prefSet(key, value); }, [key, value]);
  return [value, setValue];
}

/* ---- 章节体检抽屉 ----
   右栏只在 ≤1360 是抽屉（页头「体检」按钮拉出来，ws-author.css 的同一个断点）。抽屉开着时窗口变宽（最大化、缩放、
   收起开发者工具），右栏回到静态一列：「体检」按钮、关闭按钮、遮罩全都隐藏了，焦点陷阱却还开着——Tab 永远落在
   那个看不见的关闭按钮上，也关不掉。所以断点一过就把抽屉关上。jsdom 没有 matchMedia：没有就不管。 */
export const ARR_CTX_DRAWER_QUERY = "(max-width: 1360px)";

export function useCtxDrawer() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open || typeof window.matchMedia !== "function") return undefined;
    let mq;
    try { mq = window.matchMedia(ARR_CTX_DRAWER_QUERY); } catch (e) { return undefined; }
    if (!mq) return undefined;
    const onChange = () => { if (!mq.matches) setOpen(false); };
    onChange();   // 打开的那一刻已经宽过断点（抽屉根本不该存在）也一样收掉
    if (mq.addEventListener) mq.addEventListener("change", onChange); else if (mq.addListener) mq.addListener(onChange);
    return () => { if (mq.removeEventListener) mq.removeEventListener("change", onChange); else if (mq.removeListener) mq.removeListener(onChange); };
  }, [open]);
  return [open, setOpen];
}

/* ---- 跨视图跳转：go(view, intents) 是外壳给的；单测里没有外壳，就排队意图、改 hash（同一组意图，同一个落点） ---- */
export function arrGoView(go, view, intents) {
  const list = (Array.isArray(intents) ? intents : [intents]).filter((intent) => intent && intent.type);
  if (go) return list.length ? go(view, list) : go(view);
  if (!list.length) { window.location.hash = "#" + view; return true; }
  list.slice(0, -1).forEach((intent) => queueViewIntent(view, intent.type, intent.detail));
  const last = list[list.length - 1];
  return navigateWithViewIntent(view, last.type, last.detail);
}

/* 每一场都得有 sid（行内编辑与重排靠它认人）。目录 store 已经给乐观新建的场补了临时 sid，这里兜本页新加的场。 */
export const arrStampIds = (list) => list.map((c) => ({
  ...c,
  scenes: (c.scenes || []).map((s, i) => (s.sid ? s : { ...s, sid: c.id + "-s" + i + "-" + Math.random().toString(36).slice(2, 6) })),
}));

/* ---- 本页的章表 ----
   本地 state 只负责拖拽中的即时视图；commit 把改动交给 WsCatalog（乐观写 + 服务端收敛），
   publishDraggedOrder 在松手时把拖出来的顺序交出去。 */
export function useArrChapterList(catalogChapters) {
  const [chapters, setChapters] = useState(() => arrStampIds(catalogChapters || WsCatalog.get()));
  const chaptersRef = useRef(chapters);
  chaptersRef.current = chapters;
  const commit = (recipe) => {
    const base = arrStampIds(WsCatalog.get());
    const next = arrStampIds(typeof recipe === "function" ? recipe(base) : recipe);
    setChapters(next);
    WsCatalog.set(next);
    return next;
  };
  const publishDraggedOrder = () => { WsCatalog.set(arrStampIds(chaptersRef.current)); };
  const reload = (list) => setChapters(arrStampIds(Array.isArray(list) ? list : WsCatalog.get()));
  return { chapters, setChapters, chaptersRef, commit, publishDraggedOrder, reload };
}

/* ---- 多选（章与场共用）----
   只活在本次会话里，不落 localStorage：选择是瞬时意图，跨刷新保留只会让作者对着一份看不见来路的勾选下手 */
export function useSelection() {
  const [mode, setMode] = useState(false);
  const [sel, setSel] = useState(() => new Set());
  const toggle = useCallback((id) => setSel((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);
  return {
    mode,
    sel,
    has: (id) => sel.has(id),
    toggle,
    enter: () => { setMode(true); setSel(new Set()); },
    exit: () => { setMode(false); setSel(new Set()); },
    toggleMode: () => { setMode((v) => !v); setSel(new Set()); },
    /* 全选 / 取消全选：已经全选时再按一下就清空 */
    toggleAll: (ids) => setSel((prev) => (prev.size === ids.length ? new Set() : new Set(ids))),
    /* 目录换了：把指向已消失章 / 场的勾选摘掉，避免对着幽灵 id 再发一次删除 */
    prune: (live) => setSel((prev) => {
      const kept = new Set([...prev].filter((id) => live.has(id)));
      return kept.size === prev.size ? prev : kept;
    }),
  };
}

/* ---- 构思 → 目录 ----
   回流：把构思 9/10 步的改动写回本作目录场景卡（三拍 / POV / 章 brief）。能力来自全局 SnowSync（与构思页「重新同步」
   同源）；resync 内部已重拉 WsCatalog，这里再把最新目录灌回本页。pending 来自后端 resync_status（真相），不写死。
   canPlan：页面上给不给「整理章节结构」这扇门。构思的闸门此刻没过（某一步被改动、待重新确认）也要给——目录里
   已经有构思分出来的章，它们在这里不能拖，门不能跟着消失；面板自己会列出没过的那几项并带你去补。 */
const readSnowResync = () => { try { return (window.SnowSync && window.SnowSync.resyncStatus()) || { pendingCount: 0 }; } catch (e) { return { pendingCount: 0 }; } };
const readSnowReady = () => { try { return !!(window.SnowSync && window.SnowSync.readyToMaterialize && window.SnowSync.readyToMaterialize()); } catch (e) { return false; } };

export function useAuthorSnow({ chapters, reload, showNotice, notifyError, goView }) {
  const [resync, setResync] = useState(readSnowResync);
  const [busy, setBusy] = useState(false);
  const refreshResync = useCallback(() => setResync(readSnowResync()), []);
  useEffect(() => {
    const events = ["ws:snow-resync", "ws:snow-hydrated", "ws:work-changed"];
    events.forEach((name) => window.addEventListener(name, refreshResync));
    return () => events.forEach((name) => window.removeEventListener(name, refreshResync));
  }, [refreshResync]);
  const ready = readSnowReady();
  const hasPlanChapters = chapters.some(arrIsPlanChapter);

  const sync = async () => {
    if (busy) return;
    if (!window.SnowSync || !window.SnowSync.resync) { notifyError("同步能力还没准备好：请刷新页面，或先到「构思」里「整理章节结构」。"); return; }
    if (!ready && !hasPlanChapters) {   // 从没走过物化主路径：暂无可回流的场，引导去构思页
      const goSnow = await wsConfirm({
        title: "这部作品还没从构思整理过章节结构",
        body: "目录里还没有可以回流的场。现在去构思页「整理章节结构」吗？",
        confirmLabel: "去构思",
      });
      if (goSnow) goView("snowflake");
      return;
    }
    setBusy(true);
    try {
      const r = await window.SnowSync.resync();                  // POST /resync，内部已 WsCatalog.__refresh
      reload();                                                   // 订阅也会收敛；这里让结果即时可见
      refreshResync();
      showNotice({ text: (r && r.synced) ? `已把 ${r.synced} 场的构思改动同步到目录` : "目录已经是最新的，没有要同步的", tone: (r && r.synced) ? "ok" : "neutral" });
    } catch (e) {
      notifyError("从构思同步没有成功：" + ((e && e.message) || "请稍后重试，或检查构思各步是否已确认。"));
    } finally { setBusy(false); }
  };
  /* 体检、构思条、清单都拿这一份 snow：回调走 ref，对象本身只在值变了时才换（全书体检的 useMemo 才命中得了） */
  const syncRef = useRef(sync);
  syncRef.current = sync;
  const onSync = useCallback(() => syncRef.current(), []);
  const pending = (resync && resync.pendingCount) || 0;
  const snow = useMemo(() => ({
    pending, busy, ready, canPlan: ready || hasPlanChapters, onSync,
  }), [pending, busy, ready, hasPlanChapters, onSync]);
  return { snow, refreshResync };
}

/* ---- 键盘挪位之后把焦点还给那一行 ----
   React 换位时会把某一行的 DOM 节点挪走（换卷时整行重建），挪走的节点失焦；按一下方向键焦点就掉到 body 上，
   键盘挪位就只能挪一格。挪完按 data-arr-move 找回那一行的抓手（或序列栏的行）再聚焦。 */
function useRefocusAfterMove() {
  const pending = useRef(null);
  useEffect(() => {
    const key = pending.current;
    if (!key) return;
    pending.current = null;
    const node = [...document.querySelectorAll("[data-arr-move]")].find((el) => el.getAttribute("data-arr-move") === key);
    if (node && document.activeElement !== node) node.focus();
  });
  return (key) => { pending.current = key; };
}

/* ---- 章的拖动 / 挪位 ---- */
const approvedPositionsStayFixed = (before, after) => before.every((chapter, index) => (
  chapter.state !== "approved" || (after[index] && after[index].id === chapter.id)
));
/* 阶段 Z：构思分出来的章彼此的先后与卷由章表决定——这里不给拖（后端同样 409）；已批准终稿的位置锁定。手建的章照常拖 */
const chapterFixed = (item) => !item || item.state === "approved" || arrIsPlanChapter(item);

export function useChapterDnd({ chapters, setChapters, chaptersRef, commit, publishDraggedOrder, notifyError }) {
  const [dragId, setDragId] = useState(null);
  const refocus = useRefocusAfterMove();
  const find = (id) => chapters.find((item) => item.id === id);

  /* 拖一章：跨卷时它归目标那一卷，落在目标的位置上 */
  const row = (id) => ({
    draggable: !chapterFixed(find(id)),
    onDragStart: (e) => {
      if (chapterFixed(find(id))) { e.preventDefault(); return; }
      setDragId(id); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", id); } catch (_) {}
    },
    onDragEnter: () => {
      if (!dragId || dragId === id) return;
      if (chapterFixed(find(dragId))) return;
      setChapters((cs) => {
        const from = cs.findIndex((c) => c.id === dragId);
        const to = cs.findIndex((c) => c.id === id);
        if (from < 0 || to < 0 || from === to) return cs;
        const arr = [...cs];
        const moved = { ...arr[from], act: arr[to].act };
        arr.splice(from, 1);
        arr.splice(arr.findIndex((c) => c.id === id), 0, moved);
        if (!approvedPositionsStayFixed(cs, arr)) return cs;
        chaptersRef.current = arr;
        return arr;
      });
    },
    onDragOver: (e) => e.preventDefault(),
    onDrop: (e) => { e.preventDefault(); publishDraggedOrder(); setDragId(null); },
    onDragEnd: () => { publishDraggedOrder(); setDragId(null); },
    "data-dragging": dragId === id ? "true" : undefined,
  });

  /* 落在一卷的空白处 → 挪到那一卷最后 */
  const board = (actId) => ({
    onDragOver: (e) => e.preventDefault(),
    onDrop: (e) => {
      e.preventDefault();
      const current = dragId;
      setDragId(null);
      if (!current) return;
      const cs = chaptersRef.current;
      const cur = cs.find((c) => c.id === current);
      if (!cur || chapterFixed(cur) || cur.act === actId) return;
      const arr = cs.filter((c) => c.id !== current);
      let insertAt = arr.length;
      for (let i = arr.length - 1; i >= 0; i--) { if (arr[i].act === actId) { insertAt = i + 1; break; } }
      arr.splice(insertAt, 0, { ...cur, act: actId });
      if (!approvedPositionsStayFixed(cs, arr)) return;
      chaptersRef.current = arr;
      commit(arr);
    },
  });

  /* 键盘挪章：卷内和相邻的章换位；到了卷首 / 卷尾再按一下就归到上一卷 / 下一卷 */
  const move = (id, dir) => {
    const cs = chaptersRef.current;
    const at = cs.findIndex((c) => c.id === id);
    const cur = cs[at];
    if (!cur || chapterFixed(cur)) return;
    const actAt = ARR_ACTS.findIndex((a) => a.id === cur.act);
    const neighbor = cs[at + dir];
    let arr;
    if (neighbor && neighbor.act === cur.act) {
      arr = [...cs];
      arr[at] = neighbor; arr[at + dir] = cur;
    } else {
      const nextAct = neighbor ? neighbor.act : (ARR_ACTS[actAt + dir] || {}).id;
      if (!nextAct || nextAct === cur.act) return;
      arr = cs.map((c) => (c.id === id ? { ...c, act: nextAct } : c));
    }
    if (!approvedPositionsStayFixed(cs, arr)) { notifyError("已批准终稿的章位置锁定，别的章不能越过它。"); return; }
    chaptersRef.current = arr;
    refocus("ch:" + id);
    commit(arr);
  };

  return { dragId, row, board, move, canMove: (c) => !chapterFixed(c) };
}

/* ---- 当前章里的场：拖动 / 挪位 ----
   雪花的场彼此的先后 = 构思第 9 步的行序：它们的抓手只是标记（ArrGrip 不给 dnd）；手加的场可以拖到任何两场之间，
   键盘上和相邻一场换位（雪花的场彼此的先后不变，后端的 scene-order 校验照样过）。 */
export function useSceneDnd({ ch, setChapters, chaptersRef, commit, publishDraggedOrder }) {
  const [dragIdx, setDragIdx] = useState(null);
  const refocus = useRefocusAfterMove();
  const locked = !!ch && ch.state === "approved";

  const handle = (i) => ({
    draggable: !locked,
    onDragStart: (e) => {
      if (locked) { e.preventDefault(); return; }
      setDragIdx(i); e.dataTransfer.effectAllowed = "move"; e.stopPropagation();
    },
    onDragEnd: () => { publishDraggedOrder(); setDragIdx(null); },
  });
  const zone = (i) => ({
    onDragEnter: () => {
      if (!ch || dragIdx == null || dragIdx === i || locked) return;
      setChapters((cs) => {
        const nextList = cs.map((c) => {
          if (c.id !== ch.id) return c;
          const s = [...c.scenes]; const m = s.splice(dragIdx, 1)[0]; s.splice(i, 0, m);
          return { ...c, scenes: s };
        });
        chaptersRef.current = nextList;
        return nextList;
      });
      setDragIdx(i);
    },
    onDragOver: (e) => e.preventDefault(),
    onDrop: (e) => { e.preventDefault(); publishDraggedOrder(); setDragIdx(null); },
    "data-dragging": dragIdx === i ? "true" : undefined,
  });
  const move = (i, dir) => {
    if (!ch || locked) return;
    const j = i + dir;
    if (j < 0 || j >= ch.scenes.length || arrIsPlanScene(ch.scenes[i])) return;
    refocus("sc:" + ch.scenes[i].sid);
    commit((cs) => cs.map((c) => {
      if (c.id !== ch.id) return c;
      const s = [...c.scenes]; [s[i], s[j]] = [s[j], s[i]];
      return { ...c, scenes: s };
    }));
  };
  return { handle, zone, move };
}
