import React from "react";
import { I } from "./icons.jsx";
import { LIB_CATS } from "./labels/library.js";
import { LIB_REL_TYPES } from "./ws-library-derive.js";
import { GH, GW, buildEdges, computeLayout } from "./ws-library-graph-layout.js";
import { LibEntryRow, libCatLabel } from "./ws-library-parts.jsx";
import { wsKey } from "./ws-works.jsx";
import { EmptyState, IconButton, Popover, Segmented } from "./ws-ui.jsx";

const { useMemo, useState, useRef, useEffect, useLayoutEffect } = React;

/* ==========================================================
   资料 · 关系图谱（确定性的力导向布局）
   · 布局只在「有哪些档案、谁和谁连着」变了时重算（结构键），置顶、改简述、刷新都不再重跑 O(n²) 的模拟；
   · 布局算在抽象坐标里（GW × GH，拖拽记忆 ws-lib-graph-pos-v1 也存在这个坐标里），
     画的时候按舞台的实际像素铺开：viewBox 就是舞台尺寸，字号等于 CSS 字号，不再被整体缩到七成；
   · 滚轮缩放用原生的非被动监听（React 的 onWheel 是被动的，preventDefault 拦不住页面滚动）；
   · 节点可以用 Tab 走到，回车打开档案、空格选中；
   · 工具栏只有一行：查找、图例（只列数据里真有的类别和关系）、「筛选」弹层。
   ========================================================== */

/* 舞台四周留给节点半径和名字的像素边距（名字写在节点下方，所以下边多留一点） */
const PAD = { l: 48, r: 48, t: 40, b: 56 };
const POS_KEY = "ws-lib-graph-pos-v1";
const posKey = () => wsKey(POS_KEY);

const readSavedPos = () => {
  try { return JSON.parse(localStorage.getItem(posKey()) || "{}") || {}; } catch (e) { return {}; }
};
const writeSavedPos = (next) => { try { localStorage.setItem(posKey(), JSON.stringify(next)); } catch (e) {} };

/* 缩小按钮用的「−」：图标集里没有 Minus */
const MinusIcon = ({ size = 15 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true"><path d="M5 12h14" /></svg>
);

const toggleIn = (set, id) => { const n = new Set(set); if (n.has(id)) n.delete(id); else n.add(id); return n; };

/* 「筛选」弹层：只列数据里出现过的类别和关系类型；名字标签全部显示或只在聚焦时显示。
   开合、Esc、点外面、Tab 出去都交给 ws-ui 的 Popover（usePopover）——以前这里自己挂 pointerdown / Esc。 */
function GraphFilter({ cats, rels, catCount, offCats, offRels, labelMode, onCats, onRels, onLabelMode }) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef(null);
  const hidden = offCats.size + offRels.size;

  return (
    <div className="graph-filter">
      <button
        ref={btnRef}
        type="button"
        className={`btn btn-ghost btn-sm ${hidden ? "is-on" : ""}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen(o => !o)}
      >
        <I.Filter size={13} /> 筛选{hidden ? <span className="graph-filter-n">{hidden}</span> : null}
      </button>
      <Popover open={open} onClose={() => setOpen(false)} anchorRef={btnRef} label="筛选图谱" className="graph-pop">
        <fieldset className="graph-pop-group">
          <legend>类别</legend>
          {cats.map(c => (
            <label key={c.id} className={`graph-pop-opt acc-${c.accent}`}>
              <input type="checkbox" checked={!offCats.has(c.id)} onChange={() => onCats(toggleIn(offCats, c.id))} />
              <span className="graph-key-dot" aria-hidden="true" />
              <span className="graph-pop-label">{c.label}</span>
              <span className="graph-pop-n">{catCount[c.id] || 0}</span>
            </label>
          ))}
        </fieldset>
        {rels.length > 0 && (
          <fieldset className="graph-pop-group">
            <legend>关系</legend>
            {rels.map(t => (
              <label key={t.id} className={`graph-pop-opt acc-${t.accent}`} title={t.hint}>
                <input type="checkbox" checked={!offRels.has(t.id)} onChange={() => onRels(toggleIn(offRels, t.id))} />
                <span className="graph-key-bar" aria-hidden="true" />
                <span className="graph-pop-label">{t.label}</span>
                <span className="graph-pop-hint">{t.hint}</span>
              </label>
            ))}
          </fieldset>
        )}
        <div className="graph-pop-group">
          <div className="graph-pop-cap">名字</div>
          <Segmented
            label="名字"
            value={labelMode}
            onChange={onLabelMode}
            options={[{ value: "all", label: "全部显示" }, { value: "focus", label: "只在选中时" }]}
          />
        </div>
        <div className="graph-pop-foot">
          <button type="button" className="btn btn-quiet btn-sm" disabled={!hidden && labelMode === "all"}
            onClick={() => { onCats(new Set()); onRels(new Set()); onLabelMode("all"); }}>
            恢复默认
          </button>
        </div>
      </Popover>
    </div>
  );
}

/* 选中节点的侧栏：是谁、几项关联、按关系类型分布，一个「查看完整档案」 */
function GraphSidePanel({ entry, degree, breakdown, onClose, onOpen }) {
  const kind = entry.cat === "events" ? (entry.timeLabel || "未定时间") : entry.kind;
  return (
    <div className={`graph-panel acc-${entry.accent}`}>
      <div className="graph-panel-head">
        <LibEntryRow entry={entry} glyphSize="lg" sub={`${libCatLabel(entry.cat)} · ${kind}`} className="graph-panel-row" />
        <IconButton icon="X" label="取消选中" onClick={onClose} />
      </div>
      <div className="graph-panel-rel">
        <I.Compass size={12} aria-hidden="true" /> {degree} 项关联
      </div>
      {breakdown.length > 0 && (
        <div className="graph-panel-types">
          {breakdown.map(({ t, n }) => (
            <span key={t.id} className={`rel-chip acc-${t.accent}`} title={t.hint}>
              <span className="graph-key-bar" aria-hidden="true" />{t.label}<b>{n}</b>
            </span>
          ))}
        </div>
      )}
      <button type="button" className="btn btn-primary btn-sm" onClick={() => onOpen(entry.id)}>
        <I.BookOpen size={13} /> 查看完整档案
      </button>
    </div>
  );
}

/* 缩放 / 平移（像素坐标）：视图变换、滚轮缩放（原生、非被动的监听——只有这样 preventDefault 才拦得住页面跟着滚）、
   按钮缩放与复位，以及屏幕坐标 → 缩放平移之前的像素坐标。svg 出现 / 消失（有无关联）时重挂滚轮监听。 */
function useGraphViewport(svgRef, size, hasEdges) {
  const [view, setView] = useState({ k: 1, tx: 0, ty: 0 });
  const zoomAt = (cx, cy, factor) => setView(v => {
    const k = Math.min(3, Math.max(0.5, v.k * factor));
    return { k, tx: cx - (cx - v.tx) * (k / v.k), ty: cy - (cy - v.ty) * (k / v.k) };
  });
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return undefined;
    const onWheel = (ev) => {
      ev.preventDefault();
      const r = el.getBoundingClientRect();
      zoomAt(ev.clientX - r.left, ev.clientY - r.top, ev.deltaY < 0 ? 1.12 : 1 / 1.12);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [hasEdges]); // eslint-disable-line react-hooks/exhaustive-deps
  const toLocal = (clientX, clientY) => {
    const r = svgRef.current.getBoundingClientRect();
    return { x: (clientX - r.left - view.tx) / view.k, y: (clientY - r.top - view.ty) / view.k };
  };
  return {
    view, setView, toLocal,
    zoomBy: (f) => zoomAt(size.w / 2, size.h / 2, f),
    resetView: () => setView({ k: 1, tx: 0, ty: 0 }),
  };
}

function LibGraph({ selId, onSelect, onOpen, onBrowse, entries, byId }) {
  const ents = entries || [];
  const bid = byId || {};
  const edges = useMemo(() => buildEdges(ents, bid), [ents, bid]);
  /* 结构键：只有档案的增删 / 换类别、边的增删才重算布局 */
  const layoutKey = useMemo(
    () => ents.map(e => `${e.id}:${e.cat}`).join(",") + "|" + edges.map(e => `${e.a}>${e.b}`).join(","),
    [ents, edges]
  );
  const layout = useMemo(() => computeLayout(edges, ents), [layoutKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const hasEdges = edges.length > 0;

  /* 连接度：决定节点大小 */
  const deg = useMemo(() => {
    const d = {};
    edges.forEach(e => { d[e.a] = (d[e.a] || 0) + 1; d[e.b] = (d[e.b] || 0) + 1; });
    return d;
  }, [edges]);
  const nodeR = (id, isSel) => {
    const r = Math.min(27, 14 + Math.sqrt(deg[id] || 0) * 4.2);
    return isSel ? r + 3 : r;
  };
  const [hover, setHover] = useState(null);

  /* ---- 舞台尺寸（像素）：viewBox 跟着它走 ---- */
  const stageRef = useRef(null);
  const svgRef = useRef(null);
  const [size, setSize] = useState({ w: GW, h: GH });
  useLayoutEffect(() => {
    const el = stageRef.current;
    if (!el) return undefined;
    const measure = () => {
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return;
      const w = Math.round(r.width), h = Math.round(r.height);
      setSize(s => (s.w === w && s.h === h ? s : { w, h }));
    };
    measure();
    if (typeof ResizeObserver !== "function") return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const spanX = Math.max(1, size.w - PAD.l - PAD.r);
  const spanY = Math.max(1, size.h - PAD.t - PAD.b);
  const toPx = (p) => ({ x: PAD.l + (p.x / GW) * spanX, y: PAD.t + (p.y / GH) * spanY });
  const fromPx = (p) => ({ x: ((p.x - PAD.l) / spanX) * GW, y: ((p.y - PAD.t) / spanY) * GH });

  /* ---- 拖拽重排的节点位置（本机记忆，按作品隔离） ---- */
  const [posOv, setPosOv] = useState(readSavedPos);
  const pos = useMemo(() => ({ ...layout, ...posOv }), [layout, posOv]);
  const hasCustom = Object.keys(posOv).length > 0;
  const resetLayout = () => { setPosOv({}); writeSavedPos({}); };

  const focus = hover || selId;
  const neighbours = useMemo(() => {
    const set = new Set();
    if (!focus) return set;
    edges.forEach(e => {
      if (e.a === focus) set.add(e.b);
      if (e.b === focus) set.add(e.a);
    });
    return set;
  }, [focus, edges]);

  const sel = bid[selId];

  /* ---- 筛选（只列数据里出现过的） ---- */
  const presentCats = useMemo(() => LIB_CATS.filter(c => ents.some(e => e.cat === c.id)), [ents]);
  const catCount = useMemo(() => ents.reduce((m, e) => { m[e.cat] = (m[e.cat] || 0) + 1; return m; }, {}), [ents]);
  const presentRels = useMemo(() => LIB_REL_TYPES.filter(t => edges.some(e => e.typeId === t.id)), [edges]);
  const [offCats, setOffCats] = useState(() => new Set());
  const [offRels, setOffRels] = useState(() => new Set());
  const [labelMode, setLabelMode] = useState("all");
  const catOn = (id) => !offCats.has(id);
  const relOn = (id) => !offRels.has(id);

  /* ---- 查找 ---- */
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const matchSet = useMemo(() => {
    const s = new Set();
    if (!q) return s;
    ents.forEach(e => {
      const hay = [e.name, e.code, e.kind, ...(e.tags || [])].join(" ").toLowerCase();
      if (hay.includes(q)) s.add(e.id);
    });
    return s;
  }, [q, ents]);
  const nodeVisible = (e) => catOn(e.cat);
  const edgeVisible = (e) => catOn(bid[e.a]?.cat) && catOn(bid[e.b]?.cat) && relOn(e.typeId);

  /* 选中节点的关系类型分布 */
  const selRelBreakdown = useMemo(() => {
    if (!selId) return [];
    const cnt = {};
    edges.forEach(e => {
      if (e.a !== selId && e.b !== selId) return;
      cnt[e.typeId] = (cnt[e.typeId] || 0) + 1;
    });
    return LIB_REL_TYPES.map(t => ({ t, n: cnt[t.id] || 0 })).filter(x => x.n);
  }, [selId, edges]);
  /* 面板说的是选中的那一个：数它自己的边。neighbours 跟着悬停 / 键盘焦点走，只管暗化与高亮 */
  const selDegree = selRelBreakdown.reduce((n, x) => n + x.n, 0);

  /* ---- 缩放 / 平移（像素坐标） ---- */
  const { view, setView, toLocal, zoomBy, resetView } = useGraphViewport(svgRef, size, hasEdges);
  const [dragMode, setDragMode] = useState(null); /* null | "pan" | 节点 id */
  const drag = useRef(null);
  const ndrag = useRef(null);
  const clickGuard = useRef(false);

  const onDown = (ev) => {
    if (ev.button !== 0) return;
    clickGuard.current = false;
    drag.current = { sx: ev.clientX, sy: ev.clientY, tx: view.tx, ty: view.ty, moved: false };
  };
  const onMove = (ev) => {
    if (ndrag.current) {
      const loc = toLocal(ev.clientX, ev.clientY);
      const id = ndrag.current.id;
      const np = fromPx({ x: loc.x + ndrag.current.ox, y: loc.y + ndrag.current.oy });
      if (!ndrag.current.moved) setDragMode(id);
      ndrag.current.moved = true;
      setPosOv(prev => ({ ...prev, [id]: np }));
      return;
    }
    if (!drag.current) return;
    const dx = ev.clientX - drag.current.sx;
    const dy = ev.clientY - drag.current.sy;
    if (!drag.current.moved && Math.abs(dx) + Math.abs(dy) > 3) { drag.current.moved = true; setDragMode("pan"); }
    if (!drag.current.moved) return;
    const start = drag.current;
    setView(v => ({ ...v, tx: start.tx + dx, ty: start.ty + dy }));
  };
  const endDrag = () => {
    if (ndrag.current && ndrag.current.moved) {
      clickGuard.current = true;
      setPosOv(prev => { writeSavedPos(prev); return prev; });
    }
    ndrag.current = null;
    if (drag.current && drag.current.moved) clickGuard.current = true;
    drag.current = null;
    setDragMode(null);
  };
  const startNodeDrag = (ev, id) => {
    ev.stopPropagation();
    if (ev.button !== 0) return;
    clickGuard.current = false;
    const loc = toLocal(ev.clientX, ev.clientY);
    const cur = toPx(pos[id]);
    ndrag.current = { id, ox: cur.x - loc.x, oy: cur.y - loc.y, moved: false };
  };
  const onBgClick = () => {
    if (clickGuard.current) { clickGuard.current = false; return; }
    onSelect(null);
  };

  const isLit = (id) => {
    if (focus) return id === focus || neighbours.has(id);
    if (q) return matchSet.has(id);
    return true;
  };
  const edgeLit = (e) => {
    if (focus) return e.a === focus || e.b === focus;
    if (q) return matchSet.has(e.a) || matchSet.has(e.b);
    return true;
  };
  /* 名字标签：聚焦 / 查找时补全相关项，其余看「名字」选项 */
  const showLabel = (id) => {
    if (!isLit(id)) return false;
    if (focus) return true;
    if (q) return matchSet.has(id);
    return labelMode === "all";
  };

  const onNodeKeyDown = (ev, id) => {
    if (ev.key === "Enter") { ev.preventDefault(); onOpen(id); }
    else if (ev.key === " " || ev.key === "Spacebar") { ev.preventDefault(); onSelect(id === selId ? null : id); }
  };
  const onStageKeyDown = (ev) => {
    if (ev.key === "Escape" && selId) { ev.preventDefault(); onSelect(null); }
  };

  return (
    <div className={`lib2-graph ${hasEdges ? "" : "is-empty"}`}>
      {hasEdges && (
        <div className="lib2-graph-bar">
          <div className="graph-search">
            <span className="graph-search-ic" aria-hidden="true"><I.Search size={14} /></span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="在图谱中查找…"
              aria-label="在图谱中查找"
              spellCheck={false}
            />
            {query && (
              <button type="button" className="graph-search-clear" onClick={() => setQuery("")} aria-label="清空查找" title="清空查找"><I.X size={12} /></button>
            )}
          </div>
          <ul className="graph-legend" aria-label="图例">
            {presentCats.map(c => (
              <li key={c.id} className={`graph-legend-item acc-${c.accent} ${catOn(c.id) ? "" : "is-off"}`}>
                <span className="graph-key-dot" aria-hidden="true" />{c.label}
              </li>
            ))}
            {presentRels.map(t => (
              <li key={t.id} className={`graph-legend-item acc-${t.accent} ${relOn(t.id) ? "" : "is-off"}`} title={t.hint}>
                <span className="graph-key-bar" aria-hidden="true" />{t.label}
              </li>
            ))}
          </ul>
          <span className="graph-bar-spacer" />
          <GraphFilter
            cats={presentCats} rels={presentRels} catCount={catCount}
            offCats={offCats} offRels={offRels} labelMode={labelMode}
            onCats={setOffCats} onRels={setOffRels} onLabelMode={setLabelMode}
          />
        </div>
      )}

      <div className="lib2-graph-stage" ref={stageRef} onKeyDown={onStageKeyDown}>
        {!hasEdges ? (
          <EmptyState
            icon="Compass"
            title="还没有关联"
            actions={onBrowse ? <button type="button" className="btn btn-ghost btn-sm" onClick={onBrowse}><I.Layout size={13} /> 去档案里添加关系</button> : null}
          >
            在档案里添加关系后，这里会连成网。
          </EmptyState>
        ) : (
          <>
            <svg
              ref={svgRef}
              className={`lib2-graph-svg ${dragMode === "pan" ? "is-panning" : ""}`}
              viewBox={`0 0 ${size.w} ${size.h}`}
              role="group"
              aria-label="关系图谱"
              onMouseDown={onDown}
              onMouseMove={onMove}
              onMouseUp={endDrag}
              onMouseLeave={endDrag}
            >
              {/* 点空白处取消选中 */}
              <rect x="0" y="0" width={size.w} height={size.h} fill="transparent" onClick={onBgClick} />
              <g transform={`translate(${view.tx} ${view.ty}) scale(${view.k})`}>
                <g>
                  {edges.map((e, i) => {
                    if (!edgeVisible(e)) return null;
                    const p = toPx(pos[e.a]), q2 = toPx(pos[e.b]);
                    return (
                      <line key={i} x1={p.x} y1={p.y} x2={q2.x} y2={q2.y}
                        className={`graph-edge rel-${e.typeId} ${edgeLit(e) ? "is-lit" : "is-faint"}`} />
                    );
                  })}
                </g>
                {focus && (
                  <g>
                    {edges.filter(e => edgeLit(e) && edgeVisible(e)).map((e, i) => {
                      const other = e.a === focus ? e.b : e.a;
                      const p = toPx(pos[focus]), q2 = toPx(pos[other]);
                      return (
                        <text key={i} x={(p.x + q2.x) / 2} y={(p.y + q2.y) / 2 - 4} className="graph-edge-label" textAnchor="middle">{e.rel}</text>
                      );
                    })}
                  </g>
                )}
                <g>
                  {ents.map(e => {
                    if (!nodeVisible(e)) return null;
                    const p = toPx(pos[e.id]);
                    const isSel = e.id === selId;
                    const r = nodeR(e.id, isSel);
                    const gf = Math.round(r * 0.82);
                    const cls = [
                      "graph-node", `acc-${e.accent}`,
                      isLit(e.id) ? "is-lit" : "is-dim",
                      isSel && "is-sel",
                      q && matchSet.has(e.id) && "is-match",
                      dragMode === e.id && "is-dragging",
                    ].filter(Boolean).join(" ");
                    return (
                      <g key={e.id}
                        className={cls}
                        transform={`translate(${p.x} ${p.y})`}
                        role="button"
                        tabIndex={0}
                        aria-pressed={isSel}
                        aria-label={`${e.name}，${libCatLabel(e.cat)}，${deg[e.id] || 0} 项关联。回车打开档案，空格选中`}
                        onMouseEnter={() => setHover(e.id)}
                        onMouseLeave={() => setHover(null)}
                        onFocus={() => setHover(e.id)}
                        onBlur={() => setHover(null)}
                        onKeyDown={(ev) => onNodeKeyDown(ev, e.id)}
                        onMouseDown={(ev) => startNodeDrag(ev, e.id)}
                        onClick={(ev) => { ev.stopPropagation(); if (clickGuard.current) { clickGuard.current = false; return; } onSelect(e.id); }}
                        onDoubleClick={(ev) => { ev.stopPropagation(); onOpen(e.id); }}>
                        <circle className="graph-node-halo" r={r + 7} />
                        <circle className="graph-node-dot" r={r} />
                        <text className="graph-node-glyph" textAnchor="middle" dy={Math.round(gf * 0.34)} fontSize={gf}>{e.glyph}</text>
                        {showLabel(e.id) && (
                          <text className="graph-node-label" textAnchor="middle" y={r + 16}>{e.name}</text>
                        )}
                      </g>
                    );
                  })}
                </g>
              </g>
            </svg>

            <div className="graph-zoom" role="group" aria-label="缩放">
              <IconButton icon="Plus" label="放大" onClick={() => zoomBy(1.2)} />
              <IconButton icon={MinusIcon} label="缩小" onClick={() => zoomBy(1 / 1.2)} />
              <IconButton icon="Refresh" label="复位视图" onClick={resetView} />
              {hasCustom && <IconButton icon="Grid" label="复位布局（清除拖拽过的位置）" onClick={resetLayout} className="graph-zoom-layout" />}
            </div>

            {sel && (
              <GraphSidePanel entry={sel} degree={selDegree} breakdown={selRelBreakdown} onClose={() => onSelect(null)} onOpen={onOpen} />
            )}

            {q && (
              <div className="graph-searchstat" role="status">
                {matchSet.size > 0 ? <>找到 <b>{matchSet.size}</b> 份，其余已淡出</> : <>没有匹配「{query}」的档案</>}
              </div>
            )}

            <div className="graph-hint"><I.Info size={12} aria-hidden="true" /> 滚轮缩放，拖空白处平移，拖节点重排；Tab 走到节点，回车打开</div>
          </>
        )}
      </div>
    </div>
  );
}

export { LibGraph };
