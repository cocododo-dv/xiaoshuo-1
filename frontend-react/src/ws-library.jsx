import React from "react";
import { I } from "./icons.jsx";
import { useCatalogChapters } from "./ws-catalog.jsx";
import { LIB_CATS, LIB_ENTRIES, libLoadState, libRefetch, libSnapshot, libSubscribe } from "./ws-library-data.jsx";
import {
  LIB_SORTS, LIB_buildBacklinks, LIB_connections, LIB_entrySub, LIB_overviewFacts, LIB_sortWithPin,
} from "./ws-library-derive.jsx";
import { Dossier, DossierNav } from "./ws-library-dossier.jsx";
import { LibGraph } from "./ws-library-graph.jsx";
import { LibTimeline } from "./ws-library-timeline.jsx";
import { LibOverview } from "./ws-library-overview.jsx";
import { LibEntryRow, libCatLabel } from "./ws-library-parts.jsx";
import { DossierCreate, DossierEdit, LIB_createEntry, LIB_deleteEntry, LIB_migrateLegacy, LIB_persist } from "./ws-library-edit.jsx";
import { WsWorks, useActiveWorkIdentity } from "./ws-works.jsx";
import { setViewIntentTargetReady } from "./ws-view-intents.js";
import { wsConfirm } from "./ws-notify.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { EmptyState, Notice, PageHeader, Segmented, Spinner } from "./ws-ui.jsx";

const {
  useState: useLb,
  useMemo: useLbMemo,
  useRef: useLbRef,
  useEffect: useLbEffect,
  useSyncExternalStore: useLbExternalStore,
} = React;

/* ==========================================================
   资料 · 故事圣经（左目录 | 右详情），另有图谱与时间线两种看法。
   数据只有一份：ws-library-data.jsx 从后端装载的 LIB_ENTRIES；这里不再叠本地覆盖层——
   新建先落后端再选中，编辑保存后以服务端为准刷新。
   这个文件只管页面：目录、筛选、选中与编辑态的切换。档案阅读视图在 ws-library-dossier.jsx，
   编辑 / 新建表单在 ws-library-edit.jsx，共用的行与字块在 ws-library-parts.jsx；回收站在 ws-trash.jsx。
   ========================================================== */

const VIEW_OPTIONS = [
  { value: "files", label: "档案", icon: <I.Layout size={14} /> },
  { value: "graph", label: "图谱", icon: <I.Compass size={14} /> },
  { value: "timeline", label: "时间线", icon: <I.Clock size={14} /> },
];

/* 列表里的方向键只在焦点落在某一条上时接管；输入框、下拉框里的方向键原样交给它们。 */
const isFormField = (el) => !!(el && el.closest && el.closest("input, textarea, select, [contenteditable='true']"));

function WsLibrary({ go }) {
  const [query, setQuery]   = useLb("");
  const [cat, setCat]       = useLb("all");
  const [selId, setSelId]   = useLb(null);          /* null → 总览落地页 */
  const [vmode, setVmode]   = useLb("files");
  const [sort, setSort]     = useLb("recent");
  const [editing, setEditing] = useLb(false);
  const [creating, setCreating] = useLb(false);
  const [pane, setPane]     = useLb("list");        /* 窄屏（≤820px）只显示一栏：list | detail */
  const [pinOverride, setPinOverride] = useLb({});   /* 置顶的乐观值，服务端刷新后撤掉 */
  const listRef = useLbRef(null);
  const pendingEdit = useLbRef(null);   /* 新建后自动进入编辑态的目标 id */
  const editDirty = useLbRef(false);    /* 编辑表单是否有没保存的改动（DossierEdit 回报） */
  const libraryRevision = useLbExternalStore(libSubscribe, libSnapshot, libSnapshot);
  const chapters = useCatalogChapters ? useCatalogChapters() : [];
  const work = useActiveWorkIdentity ? useActiveWorkIdentity() : (WsWorks ? WsWorks.active() : { title: "" });

  const entries = useLbMemo(
    () => LIB_ENTRIES.map(e => (e.id in pinOverride ? { ...e, pinned: pinOverride[e.id] } : e)),
    [libraryRevision, pinOverride] // eslint-disable-line react-hooks/exhaustive-deps
  );
  const byId       = useLbMemo(() => entries.reduce((m, e) => { m[e.id] = e; return m; }, {}), [entries]);
  const backlinks  = useLbMemo(() => LIB_buildBacklinks(entries), [entries]);
  const facts      = useLbMemo(() => LIB_overviewFacts(entries), [entries]);

  /* counts per category, respecting the live query */
  const matches = useLbMemo(() => {
    const q = query.trim().toLowerCase();
    return entries.filter(e => {
      if (!q) return true;
      const hay = [e.name, e.summary, e.blurb, e.kind, e.timeLabel, ...(e.tags || [])].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }, [query, entries]);

  const counts = useLbMemo(() => {
    const c = { all: matches.length };
    LIB_CATS.forEach(k => { c[k.id] = matches.filter(e => e.cat === k.id).length; });
    return c;
  }, [matches]);

  const filtered = useLbMemo(
    () => matches.filter(e => cat === "all" || e.cat === cat),
    [matches, cat]
  );

  /* grouped for rendering; each group sorted (置顶优先) */
  const groups = useLbMemo(() => {
    const mk = (id, items) => ({ cat: id, items: LIB_sortWithPin(items, sort) });
    if (cat !== "all") return [mk(cat, filtered)];
    return LIB_CATS
      .map(k => mk(k.id, filtered.filter(e => e.cat === k.id)))
      .filter(g => g.items.length);
  }, [filtered, cat, sort]);

  /* flat visible order = exactly what's rendered (for keyboard nav) */
  const visible = useLbMemo(() => groups.flatMap(g => g.items), [groups]);

  const sel = selId ? byId[selId] : null;
  const selConns = useLbMemo(
    () => (sel ? LIB_connections(sel, byId, backlinks) : []),
    [sel, byId, backlinks]
  );

  /* 上一条 / 下一条（沿当前可见列表顺序翻阅） */
  const selIdx = useLbMemo(() => visible.findIndex(e => e.id === selId), [visible, selId]);
  const prevEntry = selIdx > 0 ? visible[selIdx - 1] : null;
  const nextEntry = selIdx >= 0 && selIdx < visible.length - 1 ? visible[selIdx + 1] : null;

  /* 选中变化：默认退出编辑态；若是刚新建的条目则进入编辑态 */
  useLbEffect(() => {
    if (pendingEdit.current && pendingEdit.current === selId) {
      pendingEdit.current = null;
      setEditing(true);
    } else {
      setEditing(false);
    }
  }, [selId]);

  /* 离开编辑态之前：有没保存的改动就先问一句（切条目、回总览、新建、换看法都走这里） */
  const leaveEdit = (fn) => {
    if (!editing || !editDirty.current) { fn(); return; }
    wsConfirm({
      title: "放弃没保存的修改？",
      body: "这份档案里还有没保存的改动，离开后就没了。",
      confirmLabel: "放弃修改",
      cancelLabel: "继续编辑",
      tone: "danger",
    }).then((ok) => { if (ok) { editDirty.current = false; fn(); } });
  };

  /* 外部跳转的监听只挂一次，经 ref 调到最新的 leaveEdit（否则拿到的是首帧的 editing=false，护不住没保存的表单） */
  const leaveEditRef = useLbRef(leaveEdit);
  leaveEditRef.current = leaveEdit;

  const openEntry = (id, { reveal = false } = {}) => leaveEdit(() => {
    setCreating(false);
    if (reveal) { setQuery(""); setCat("all"); }
    setSelId(id);
    setPane("detail");
  });
  /* 图谱 / 时间线里「打开档案」：回到档案看法，清掉筛选让这一条一定在目录里 */
  const openFromView = (id) => { setSelId(id); setCreating(false); setCat("all"); setQuery(""); setVmode("files"); setPane("detail"); };
  const goOverview = () => leaveEdit(() => { setSelId(null); setCreating(false); setEditing(false); setPane("detail"); });
  const startCreate = () => leaveEdit(() => { setVmode("files"); setCreating(true); setSelId(null); setEditing(false); setPane("detail"); });
  const switchView = (mode) => leaveEdit(() => { setVmode(mode); setEditing(false); });
  const cancelEdit = () => leaveEdit(() => setEditing(false));

  const saveEdit = async (patch) => {
    const id = selId;
    const ok = await LIB_persist({ [id]: patch });
    if (!ok) return false;
    await libRefetch();
    editDirty.current = false;
    setEditing(false);
    return true;
  };

  /* 置顶：乐观显示，写进扩展字段组 details.pinned；失败撤回 */
  const togglePin = async (entry) => {
    const next = !entry.pinned;
    setPinOverride(prev => ({ ...prev, [entry.id]: next }));
    const ok = await LIB_persist({ [entry.id]: { pinned: next } });
    if (ok) await libRefetch();
    setPinOverride(prev => { const n = { ...prev }; delete n[entry.id]; return n; });
  };

  /* 新建：先落后端，拿到服务端 id 再选中并打开编辑 */
  const doCreate = async (catId, name, options) => {
    const id = await LIB_createEntry(catId, name, options);
    if (!id) return null;
    pendingEdit.current = id;
    setCreating(false);
    setQuery("");
    setCat("all");
    setSelId(id);
    return id;
  };

  const deleteEntry = async (entry) => {
    const conns = LIB_connections(entry, byId, backlinks).length;
    const body = entry.cat === "events"
      ? "这条大事记会被删除（不进回收站），删除后不能恢复。"
      : `这份档案${conns ? `和它的 ${conns} 条关联` : ""}会一起删除（不进回收站），删除后不能恢复。${entry.cat === "people" ? "构思或场景里还在用这个人物时，删除会被拒绝。" : ""}`;
    const ok = await wsConfirm({ title: `删除「${entry.name}」？`, body, confirmLabel: "删除", tone: "danger" });
    if (!ok) return;
    const done = await LIB_deleteEntry(entry);
    if (done) { setSelId(null); setEditing(false); }
  };

  /* 旧版本机覆盖层（只存在浏览器里的改动 / 新建）一次性上行到服务端；每部作品成功一次，失败下次打开再试 */
  const workId = work && work.id;
  useLbEffect(() => { LIB_migrateLegacy(); }, [workId]);

  /* 外部跳转：从正文写作点击实体 → 打开对应档案 */
  useLbEffect(() => {
    const h = (e) => {
      const id = e.detail;
      if (!id) return;
      leaveEditRef.current(() => { setVmode("files"); setCreating(false); setQuery(""); setCat("all"); setSelId(id); setPane("detail"); });
    };
    window.addEventListener("ws:lib-open", h);
    setViewIntentTargetReady("library");
    return () => {
      setViewIntentTargetReady("library", false);
      window.removeEventListener("ws:lib-open", h);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /* 键盘：焦点在列表某一条上时，↑/↓ 翻条目，Home/End 到首尾；焦点跟着走 */
  const focusItem = (id) => {
    const run = () => {
      const root = listRef.current;
      if (!root) return;
      const el = Array.from(root.querySelectorAll(".lib2-item")).find(n => n.getAttribute("data-lib-id") === id);
      if (el) el.focus();
    };
    (window.requestAnimationFrame || ((cb) => setTimeout(cb, 0)))(run);
  };
  const onListKeyDown = (ev) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(ev.key)) return;
    if (isFormField(ev.target) || !visible.length) return;
    const item = ev.target && ev.target.closest && ev.target.closest(".lib2-item");
    if (!item) return;
    ev.preventDefault();
    const idx = visible.findIndex(e => e.id === item.getAttribute("data-lib-id"));
    let next = idx;
    if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = visible.length - 1;
    else if (ev.key === "ArrowDown") next = Math.min(visible.length - 1, idx + 1);
    else next = Math.max(0, idx - 1);
    const target = visible[next];
    if (!target || target.id === selId) return;
    openEntry(target.id);
    focusItem(target.id);
  };
  const onSearchKeyDown = (ev) => {
    if (isImeComposing(ev)) return;   // 组词时的 ↓ / Esc 属于输入法，不能跳走或清空搜索词
    if (ev.key === "ArrowDown" && visible.length) { ev.preventDefault(); focusItem((visible.find(e => e.id === selId) || visible[0]).id); }
    if (ev.key === "Escape" && query) { ev.preventDefault(); setQuery(""); }
  };
  const tabStopId = (visible.find(e => e.id === selId) || visible[0] || {}).id;

  const workTitle = (work && work.title) || "这部作品";
  const header = (
    <PageHeader
      title="故事圣经"
      description={`《${workTitle}》的人物、世界设定与大事记，彼此关联。登记过的名字在写作台里会高亮，点一下就能查。`}
      actions={(
        <>
          <Segmented label="看法" value={vmode} onChange={switchView} options={VIEW_OPTIONS} />
          <button type="button" className="btn btn-accent" onClick={startCreate}><I.Plus size={14} /> 新建档案</button>
        </>
      )}
    />
  );

  let body;
  /* 空列表有三种原因，只有「真的读到了、就是空的」才请作者新建第一份档案：
     还在读就说在读；读不到就只给一处错误和重试（不再同时摆出「还是空的」和新建按钮） */
  const load = libLoadState();
  if (entries.length === 0 && !creating && load.status === "error") {
    body = (
      <Notice tone="danger" title="档案库没有读出来" testId="library-load-error"
        actions={<button type="button" className="btn btn-ghost btn-sm" onClick={() => libRefetch()}>重试</button>}>
        {load.message}
      </Notice>
    );
  } else if (entries.length === 0 && !creating && load.status === "loading") {
    body = (
      <div className="lib2-emptypage">
        <EmptyState compact title={<><Spinner size={13} /> 正在读取档案库…</>} />
      </div>
    );
  } else if (entries.length === 0 && !creating) {
    body = (
      <div className="lib2-emptypage">
        <EmptyState
          icon="Library"
          title="这部作品的档案库还是空的"
          actions={<button type="button" className="btn btn-accent" onClick={startCreate}><I.Plus size={14} /> 新建第一份档案</button>}
        >
          人物、地点、术语在这里登记后，写作台里会自动高亮，写的时候随手就能查。
        </EmptyState>
      </div>
    );
  } else if (vmode === "graph") {
    body = (
      <LibGraph
        entries={entries} byId={byId}
        selId={selId}
        onSelect={setSelId}
        onOpen={openFromView}
        onBrowse={() => switchView("files")}
      />
    );
  } else if (vmode === "timeline") {
    body = (
      <LibTimeline
        entries={entries} byId={byId} chapters={chapters}
        selId={selId}
        onSelect={setSelId}
        onNew={startCreate}
        onOpen={openFromView}
      />
    );
  } else {
    const catOptions = [{ value: "all", label: "全部", count: counts.all }, ...LIB_CATS.map(k => ({ value: k.id, label: k.label, count: counts[k.id] }))];
    const sortOptions = Object.keys(LIB_SORTS).map(k => ({ value: k, label: LIB_SORTS[k].label }));
    body = (
      <div className="lib2-shell" data-pane={pane}>
        {/* ---- index ---- */}
        <aside className="lib2-index" aria-label="档案目录">
          <button type="button" className={`lib2-overview-btn ${selId === null && !creating ? "is-active" : ""}`} onClick={goOverview}>
            <span className="lib2-overview-ic" aria-hidden="true"><I.Activity size={15} /></span>
            <span className="lib2-overview-tx">
              <span className="t">总览</span>
              <span className="s">待补的档案与最近改动</span>
            </span>
          </button>

          <div className="lib2-search">
            <span className="lib2-search-ic" aria-hidden="true"><I.Search size={15} /></span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onSearchKeyDown}
              placeholder="搜索人物、地点、术语…"
              aria-label="搜索档案"
              spellCheck={false}
            />
            {query && (
              <button type="button" className="lib2-search-clear" onClick={() => setQuery("")} aria-label="清空搜索" title="清空搜索">
                <I.X size={14} />
              </button>
            )}
          </div>

          <div className="lib2-cats">
            <Segmented block label="按类别筛选" value={cat} onChange={setCat} options={catOptions} />
          </div>

          <div className="lib2-sortbar">
            <span className="lib2-sortbar-k">排序</span>
            <Segmented label="排序" value={sort} onChange={setSort} options={sortOptions} />
          </div>

          <div className="lib2-list" ref={listRef} onKeyDown={onListKeyDown}>
            {visible.length === 0 && (
              <EmptyState compact icon="Search" title={query ? `没有匹配「${query}」的档案` : `还没有${cat === "all" ? "" : libCatLabel(cat)}档案`} />
            )}
            {groups.map(g => (
              <div key={g.cat} role="group" aria-label={libCatLabel(g.cat)}>
                {cat === "all" && (
                  <div className="lib2-group-label" aria-hidden="true">
                    {libCatLabel(g.cat)}
                    <span className="n">{g.items.length}</span>
                  </div>
                )}
                {g.items.map(e => {
                  const active = selId === e.id && !creating;
                  return (
                    <LibEntryRow
                      key={e.id}
                      entry={e}
                      sub={LIB_entrySub(e)}
                      pinned={e.pinned}
                      active={active}
                      className="lib2-item"
                      data-lib-id={e.id}
                      tabIndex={e.id === tabStopId ? 0 : -1}
                      aria-current={active ? "true" : undefined}
                      onClick={() => openEntry(e.id)}
                    />
                  );
                })}
              </div>
            ))}
          </div>
        </aside>

        {/* ---- detail ---- */}
        <section className="lib2-detail" aria-label="档案详情">
          <button type="button" className="lib2-back" onClick={() => leaveEdit(() => setPane("list"))}>
            <I.ChevronLeft size={15} /> 档案列表
          </button>
          {creating ? (
            <DossierCreate onCreate={doCreate} onCancel={() => { setCreating(false); setPane("list"); }} />
          ) : selId === null ? (
            <LibOverview
              facts={facts}
              onSelect={(id) => openEntry(id)}
              onPickCat={(c) => { setCat(c); setSelId(null); setPane("list"); }}
              onGoGraph={() => switchView("graph")}
            />
          ) : sel ? (
            editing
              ? <DossierEdit key={sel.id} entry={sel} allEntries={entries} byId={byId} chapters={chapters}
                  onSave={saveEdit} onCancel={cancelEdit} onDirtyChange={(d) => { editDirty.current = d; }} />
              : (
                <React.Fragment>
                  <DossierNav
                    entry={sel} pos={selIdx} total={visible.length} prev={prevEntry} next={nextEntry}
                    onHome={goOverview}
                    onCat={() => { setCat(sel.cat); setPane("list"); }}
                    onOpen={(id) => openEntry(id)}
                  />
                  <Dossier
                    entry={sel} conns={selConns} byId={byId} chapters={chapters}
                    onNav={(id) => openEntry(id, { reveal: true })}
                    onEdit={() => setEditing(true)}
                    onDelete={() => deleteEntry(sel)}
                    onTogglePin={() => togglePin(sel)}
                  />
                </React.Fragment>
              )
          ) : (
            <EmptyState icon="BookOpen" title="这份档案已经不在了"
              actions={<button type="button" className="btn btn-ghost btn-sm" onClick={goOverview}>回到总览</button>}>
              它可能刚被删除。从左侧目录另选一份。
            </EmptyState>
          )}
        </section>
      </div>
    );
  }

  return (
    <div className="lib2 page" data-screen-label="library">
      <div className="page-narrow">
        {header}
        {body}
      </div>
    </div>
  );
}

export { WsLibrary };
